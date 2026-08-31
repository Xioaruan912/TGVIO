"""Disk capacity snapshots and active-job reservations for F3."""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path


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
