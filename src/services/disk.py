"""Disk capacity snapshots and active-job reservations for F3."""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class DiskSnapshot:
    total: int
    used: int
    free: int
    reserved: int

    @property
    def available_after_reservations(self) -> int:
        return max(0, self.free - self.reserved)

    @property
    def free_percent(self) -> float:
        return (self.free / self.total * 100.0) if self.total else 0.0


@dataclass(frozen=True)
class DiskDecision:
    allowed: bool
    healthy: bool
    requested_bytes: int
    available_bytes: int
    required_free_bytes: int
    free_percent: float
    reason: str = ""


@dataclass(frozen=True)
class CleanupCandidate:
    job_id: int
    legacy_seq: int | None
    state: str
    path: str
    bytes_on_disk: int
    age_seconds: float


@dataclass(frozen=True)
class CleanupProtected:
    job_id: int
    path: str
    reason: str
    bytes_on_disk: int


@dataclass(frozen=True)
class CleanupPlan:
    candidates: tuple[CleanupCandidate, ...]
    protected: tuple[CleanupProtected, ...]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(item.bytes_on_disk for item in self.candidates)

    @property
    def protected_bytes(self) -> int:
        return sum(item.bytes_on_disk for item in self.protected)


class DiskManager:
    """Monitor disk headroom without deleting or rejecting by default."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        enforce: bool = False,
        min_free_bytes: int = 5 * 1024**3,
        min_free_percent: float = 10.0,
        max_cache_bytes: int = 0,
        unknown_reserve_bytes: int = 2 * 1024**3,
    ) -> None:
        self.root = Path(root).resolve()
        self.enforce = bool(enforce)
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.min_free_percent = max(0.0, float(min_free_percent))
        self.max_cache_bytes = max(0, int(max_cache_bytes))
        self.unknown_reserve_bytes = max(0, int(unknown_reserve_bytes))
        self._reservations: dict[int, int] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> DiskSnapshot:
        usage = shutil.disk_usage(self.root)
        with self._lock:
            reserved = sum(self._reservations.values())
        return DiskSnapshot(
            total=int(usage.total),
            used=int(usage.used),
            free=int(usage.free),
            reserved=int(reserved),
        )

    def reserve(self, job_key: int, requested_bytes: int | None) -> DiskDecision:
        requested = self.unknown_reserve_bytes if requested_bytes is None else max(0, int(requested_bytes))
        with self._lock:
            previous = self._reservations.get(int(job_key), 0)
            self._reservations[int(job_key)] = requested
        try:
            snap = self.snapshot()
        except Exception:
            with self._lock:
                if previous:
                    self._reservations[int(job_key)] = previous
                else:
                    self._reservations.pop(int(job_key), None)
            raise
        available = max(0, snap.free - (snap.reserved - requested))
        free_after = max(0, available - requested)
        percent_after = (free_after / snap.total * 100.0) if snap.total else 0.0
        byte_ok = free_after >= self.min_free_bytes
        pct_ok = percent_after >= self.min_free_percent
        healthy = byte_ok and pct_ok
        reasons = []
        if not byte_ok:
            reasons.append("free-bytes")
        if not pct_ok:
            reasons.append("free-percent")
        return DiskDecision(
            allowed=healthy or not self.enforce,
            healthy=healthy,
            requested_bytes=requested,
            available_bytes=available,
            required_free_bytes=self.min_free_bytes,
            free_percent=percent_after,
            reason="+".join(reasons),
        )

    def release(self, job_key: int) -> int:
        with self._lock:
            return int(self._reservations.pop(int(job_key), 0))

    def reserved_bytes(self, job_key: int | None = None) -> int:
        with self._lock:
            if job_key is not None:
                return int(self._reservations.get(int(job_key), 0))
            return int(sum(self._reservations.values()))

    def validate_managed_path(self, path: str | os.PathLike[str]) -> Path:
        resolved = Path(path).resolve()
        try:
            common = Path(os.path.commonpath((self.root, resolved)))
        except ValueError as exc:
            raise ValueError("path is outside download root") from exc
        if common != self.root:
            raise ValueError("path is outside download root")
        return resolved

    def validate_job_dir(self, path: str | os.PathLike[str]) -> Path:
        resolved = self.validate_managed_path(path)
        if resolved.parent != self.root or not resolved.name.startswith("job-"):
            raise ValueError("path is not a direct job directory")
        return resolved

    def cleanup_plan(
        self,
        inventory: Iterable[dict[str, Any]],
        *,
        now: float | None = None,
        cache_retention_hours: float = 72.0,
        failed_retention_hours: float = 168.0,
        retry_protected_job_ids: set[int] | None = None,
        webdav_protected_job_ids: set[int] | None = None,
    ) -> CleanupPlan:
        """Classify cleanup candidates without deleting anything."""
        current = time.time() if now is None else float(now)
        retry_protected = {int(value) for value in (retry_protected_job_ids or set())}
        webdav_protected = {int(value) for value in (webdav_protected_job_ids or set())}
        candidates: list[CleanupCandidate] = []
        protected: list[CleanupProtected] = []
        terminal = {"succeeded", "cancelled", "failed"}

        for row in inventory:
            job_id = int(row.get("job_id") or 0)
            path_value = str(row.get("local_dir") or "")
            if not path_value:
                continue
            try:
                job_dir = self.validate_job_dir(path_value)
            except ValueError:
                protected.append(CleanupProtected(job_id, path_value, "unsafe-path", 0))
                continue
            bytes_on_disk, has_partial = self._job_dir_size(job_dir)
            state = str(row.get("state") or "")
            reason = ""
            if state not in terminal:
                reason = "non-terminal"
            elif row.get("claim_owner") or row.get("claim_kind"):
                reason = "active-claim"
            elif job_id in retry_protected:
                reason = "runtime-retry-protected"
            elif job_id in webdav_protected:
                reason = "runtime-webdav-protected"
            elif has_partial:
                reason = "partial-file"
            else:
                next_retry_at = float(row.get("next_retry_at") or 0)
                if next_retry_at > current:
                    reason = "job-retry-window"
                backup_state = str(row.get("backup_state") or "")
                backup_next = float(row.get("backup_next_retry_at") or 0)
                if not reason and backup_state in {"running", "retrying", "failed"}:
                    reason = "backup-protected"
                if not reason and backup_next > current:
                    reason = "backup-retry-window"

            finished = float(row.get("finished_at") or row.get("updated_at") or current)
            age_seconds = max(0.0, current - finished)
            retention = (
                max(0.0, float(failed_retention_hours)) * 3600.0
                if state == "failed"
                else max(0.0, float(cache_retention_hours)) * 3600.0
            )
            if not reason and age_seconds < retention:
                reason = "retention"

            if reason:
                protected.append(
                    CleanupProtected(job_id, str(job_dir), reason, bytes_on_disk)
                )
            else:
                legacy = row.get("legacy_seq")
                candidates.append(
                    CleanupCandidate(
                        job_id=job_id,
                        legacy_seq=int(legacy) if legacy is not None else None,
                        state=state,
                        path=str(job_dir),
                        bytes_on_disk=bytes_on_disk,
                        age_seconds=age_seconds,
                    )
                )

        candidates.sort(key=lambda item: (-item.age_seconds, item.job_id))
        return CleanupPlan(tuple(candidates), tuple(protected))

    def _job_dir_size(self, job_dir: Path) -> tuple[int, bool]:
        if not job_dir.exists():
            return 0, False
        total = 0
        partial = False
        for root, dirs, files in os.walk(job_dir, followlinks=False):
            dirs[:] = [name for name in dirs if not Path(root, name).is_symlink()]
            for name in files:
                path = Path(root, name)
                if path.is_symlink():
                    continue
                if name.endswith(".part") or ".part-" in name:
                    partial = True
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return int(total), partial
