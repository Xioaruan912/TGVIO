"""Read-only runtime statistics and health snapshots for F4."""

from __future__ import annotations

import os
import platform
import resource
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeStatsSnapshot:
    uptime_seconds: float
    today_accepted: int
    today_succeeded: int
    today_failed: int
    today_cancelled: int
    total_published_bytes: int
    total_backed_up_bytes: int
    saved_upload_bytes: int
    running: int
    waiting: int
    failed: int
    cpu_percent: float | None
    memory_mb: float
    disk_used_bytes: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    disk_reserved_bytes: int
    protected_cache_bytes: int
    reclaimable_cache_bytes: int
    disk_enforce: bool
    disk_healthy: bool | None
    telegram_connected: bool
    webdav_enabled: bool
    webdav_health: str
    webdav_age_seconds: float | None
    database_ok: bool
    recent_errors: tuple[tuple[str, int], ...]


class StatsService:
    """Collect local-only runtime state without external health probes."""

    def __init__(self, pipeline: Any, repository: Any, backup: Any) -> None:
        self._pipeline = pipeline
        self._repository = repository
        self._backup = backup
        self._started = time.monotonic()
        self._last_wall = self._started
        self._last_cpu = time.process_time()

    def _cpu_percent(self) -> float | None:
        now_wall = time.monotonic()
        now_cpu = time.process_time()
        wall_delta = now_wall - self._last_wall
        cpu_delta = now_cpu - self._last_cpu
        self._last_wall = now_wall
        self._last_cpu = now_cpu
        if wall_delta <= 0:
            return None
        return max(0.0, min(100.0 * max(1, os.cpu_count() or 1), cpu_delta / wall_delta * 100.0))

    @staticmethod
    def _memory_mb() -> float:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB. macOS reports bytes; production is Linux.
        return float(usage) / 1024.0

    def _webdav_health(self) -> tuple[bool, str, float | None]:
        cfg = self._backup.config_snapshot()
        enabled = bool(cfg.get("enabled"))
        if not enabled:
            return False, "未启用", None
        logs = [item for item in self._backup.logs_snapshot().values() if isinstance(item, dict)]
        if not logs:
            return True, "未探测", None
        latest = max(logs, key=lambda item: float(item.get("ts") or 0.0))
        ts = float(latest.get("ts") or 0.0)
        files = latest.get("files") or []
        pending = any(
            isinstance(item, dict) and item.get("status") not in ("ok", "deleted")
            for item in files
        )
        age = max(0.0, time.time() - ts) if ts > 0 else None
        return True, "有待处理备份" if pending else "正常", age

    async def snapshot(self) -> RuntimeStatsSnapshot:
        database_ok = True
        try:
            stats = await self._repository.stats_snapshot()
            counts = await self._repository.count_jobs_by_state()
        except Exception:
            database_ok = False
            stats = {"today": {}, "totals": {}, "recent_errors": []}
            counts = {}

        running = sum(int(counts.get(state, 0)) for state in ("downloading", "publishing"))
        waiting = sum(
            int(counts.get(state, 0))
            for state in ("collecting", "awaiting_confirmation", "queued", "ready", "interrupted", "paused")
        )
        failed = int(counts.get("failed", 0))

        disk_used = disk_total = disk_free = None
        disk_reserved = protected = reclaimable = 0
        disk_healthy = None
        manager = getattr(self._pipeline, "disk", None)
        if manager is not None:
            try:
                snap = manager.snapshot()
                disk_used = int(snap.used)
                disk_total = int(snap.total)
                disk_free = int(snap.free)
                disk_reserved = int(snap.reserved)
                free_after = max(0, snap.free - snap.reserved)
                pct_after = (free_after / snap.total * 100.0) if snap.total else 0.0
                disk_healthy = (
                    free_after >= manager.min_free_bytes
                    and pct_after >= manager.min_free_percent
                )
                queue = getattr(self._pipeline, "job_queue", None)
                if queue is not None:
                    plan = await queue.disk_cleanup_plan()
                    if plan is not None:
                        protected = int(plan.protected_bytes)
                        reclaimable = int(plan.reclaimable_bytes)
            except Exception:
                pass

        webdav_enabled, webdav_health, webdav_age = self._webdav_health()
        today = stats.get("today") or {}
        totals = stats.get("totals") or {}
        recent_errors = tuple(
            (str(item.get("code") or "unknown"), int(item.get("count") or 0))
            for item in (stats.get("recent_errors") or [])
        )
        connected = False
        try:
            connected = bool(self._pipeline.client.is_connected())
        except Exception:
            connected = False
        return RuntimeStatsSnapshot(
            uptime_seconds=max(0.0, time.monotonic() - self._started),
            today_accepted=int(today.get("accepted_jobs") or 0),
            today_succeeded=int(today.get("succeeded_jobs") or 0),
            today_failed=int(today.get("failed_jobs") or 0),
            today_cancelled=int(today.get("cancelled_jobs") or 0),
            total_published_bytes=int(totals.get("published_bytes") or 0),
            total_backed_up_bytes=int(totals.get("backed_up_bytes") or 0),
            saved_upload_bytes=int(totals.get("saved_upload_bytes") or 0),
            running=running,
            waiting=waiting,
            failed=failed,
            cpu_percent=self._cpu_percent(),
            memory_mb=self._memory_mb(),
            disk_used_bytes=disk_used,
            disk_total_bytes=disk_total,
            disk_free_bytes=disk_free,
            disk_reserved_bytes=disk_reserved,
            protected_cache_bytes=protected,
            reclaimable_cache_bytes=reclaimable,
            disk_enforce=bool(getattr(manager, "enforce", False)),
            disk_healthy=disk_healthy,
            telegram_connected=connected,
            webdav_enabled=webdav_enabled,
            webdav_health=webdav_health,
            webdav_age_seconds=webdav_age,
            database_ok=database_ok,
            recent_errors=recent_errors,
        )

    @staticmethod
    def _ffmpeg_version() -> str:
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            first = (result.stdout or "").splitlines()[0].strip()
            if first.startswith("ffmpeg version "):
                return first.split()[2][:40]
        except Exception:
            pass
        return "unknown"

    async def diagnostics_text(self) -> str:
        """Return a compact redacted diagnostic report with no user content."""
        import telethon
        import yt_dlp

        from .. import config

        snapshot = await self.snapshot()
        try:
            schema = ",".join(str(value) for value in await self._repository.schema_versions())
        except Exception:
            schema = "unknown"
        commit = str(os.environ.get("APP_COMMIT") or "unknown")[:40]
        proxy_cfg = getattr(self._pipeline, "proxy_cfg", {}) or {}
        proxy_count = len(proxy_cfg.get("proxies") or [])
        error_codes = ", ".join(code for code, _ in snapshot.recent_errors[:5]) or "none"
        return "\n".join(
            [
                "🧾 脱敏诊断",
                "──────────",
                f"commit={commit}",
                f"python={platform.python_version()}",
                f"sqlite={sqlite3.sqlite_version}",
                f"telethon={getattr(telethon, '__version__', 'unknown')}",
                f"yt-dlp={getattr(yt_dlp, 'version', None).__version__ if getattr(yt_dlp, 'version', None) else 'unknown'}",
                f"ffmpeg={self._ffmpeg_version()}",
                f"schema={schema}",
                f"uptime_seconds={int(snapshot.uptime_seconds)}",
                f"telegram_connected={str(snapshot.telegram_connected).lower()}",
                f"webdav_enabled={str(snapshot.webdav_enabled).lower()}",
                f"proxy_auto={str(bool(proxy_cfg.get('auto'))).lower()}",
                f"proxy_count={proxy_count}",
                f"cover_mode={str(bool(config.COVER_MODE)).lower()}",
                f"disk_enforce={str(snapshot.disk_enforce).lower()}",
                f"disk_healthy={str(snapshot.disk_healthy).lower() if snapshot.disk_healthy is not None else 'unknown'}",
                f"queue_running={snapshot.running}",
                f"queue_waiting={snapshot.waiting}",
                f"queue_failed={snapshot.failed}",
                f"database_ok={str(snapshot.database_ok).lower()}",
                f"recent_error_codes={error_codes}",
            ]
        )
