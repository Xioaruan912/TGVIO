"""Privacy-preserving read models for the localhost O1 dashboard."""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from .stats import RuntimeStatsSnapshot, StatsService


_SAFE_CODE = re.compile(r"[^a-zA-Z0-9_.:-]")
_RUNNING = frozenset({"downloading", "publishing"})
_WAITING = frozenset(
    {"collecting", "awaiting_confirmation", "queued", "ready", "interrupted", "paused"}
)


def _code(value: object, *, fallback: str = "unknown", limit: int = 64) -> str:
    cleaned = _SAFE_CODE.sub("_", str(value or ""))[:limit]
    return cleaned or fallback


def _label(value: object, *, limit: int = 80) -> str:
    """Keep human labels while removing controls and markup delimiters."""
    text = " ".join(str(value or "").split())[:limit]
    return text.replace("<", "‹").replace(">", "›") or "未命名"


class DashboardService:
    """Build versioned DTOs without exposing Telegram identities or user content."""

    schema_version = 1

    def __init__(self, pipeline: Any, repository: Any, stats: StatsService) -> None:
        self._pipeline = pipeline
        self._repository = repository
        self._stats = stats
        self._snapshot_lock = asyncio.Lock()
        self._cached_snapshot: RuntimeStatsSnapshot | None = None
        self._snapshot_at = 0.0

    async def _runtime_snapshot(self) -> RuntimeStatsSnapshot:
        now = time.monotonic()
        if self._cached_snapshot is not None and now - self._snapshot_at <= 1.0:
            return self._cached_snapshot
        async with self._snapshot_lock:
            now = time.monotonic()
            if self._cached_snapshot is None or now - self._snapshot_at > 1.0:
                self._cached_snapshot = await self._stats.snapshot()
                self._snapshot_at = time.monotonic()
            return self._cached_snapshot

    @staticmethod
    def _webdav_health_code(enabled: bool, value: object) -> str:
        if not enabled:
            return "disabled"
        text = str(value or "").lower()
        if value == "正常" or text == "normal":
            return "normal"
        if value == "有待处理备份" or text in {"pending", "degraded"}:
            return "pending"
        if value == "未探测" or text in {"unknown", "unprobed"}:
            return "unknown"
        return _code(value, fallback="unknown")

    @staticmethod
    def _status_group(state: str) -> str:
        if state in _RUNNING:
            return "running"
        if state in _WAITING:
            return "waiting"
        if state in {"failed", "succeeded", "cancelled"}:
            return state
        return "waiting"

    @staticmethod
    def _stage(row: dict[str, Any]) -> str:
        state = str(row.get("state") or "")
        if state == "downloading":
            return "download"
        if state == "publishing":
            return "publish"
        if state == "succeeded":
            return "done"
        if str(row.get("backup_state") or "") in {"running", "failed", "retry_wait"}:
            return "backup"
        if str(row.get("publish_state") or "") == "failed":
            return "publish"
        if state == "failed" or str(row.get("download_state") or "") == "failed":
            return "download"
        return "queued"

    @staticmethod
    def _progress(row: dict[str, Any]) -> float:
        done = max(0, int(row.get("bytes_done") or 0))
        total = max(0, int(row.get("bytes_total") or 0))
        if total > 0:
            return round(min(100.0, done / total * 100.0), 1)
        item = max(0, int(row.get("current_item") or 0))
        items = max(0, int(row.get("total_items") or 0))
        if items > 0:
            return round(min(100.0, item / items * 100.0), 1)
        return 100.0 if str(row.get("state")) == "succeeded" else 0.0

    @staticmethod
    def _job_title(kind: str, total_items: int) -> str:
        names = {
            "url": "链接任务",
            "album": "相册任务",
            "collection": "合集任务",
            "forward": "Telegram 媒体任务",
            "telegram": "Telegram 媒体任务",
        }
        base = names.get(kind, "媒体任务")
        return f"{base} / {total_items} 项" if total_items > 1 else base

    async def jobs(
        self, *, filter_name: str = "all", page: int = 0, page_size: int = 20
    ) -> dict[str, Any]:
        page = max(0, min(int(page), 10000))
        page_size = max(1, min(int(page_size), 100))
        rows, total = await self._repository.dashboard_job_page(
            filter_name=filter_name,
            page=page,
            page_size=page_size,
        )
        items = []
        for row in rows:
            kind = _code(row.get("kind"), fallback="media")
            total_items = max(0, int(row.get("total_items") or 0))
            state = _code(row.get("state"), fallback="unknown")
            items.append(
                {
                    "id": int(row["id"]),
                    "kind": kind,
                    "title": self._job_title(kind, total_items),
                    "state": state,
                    "status_group": self._status_group(state),
                    "stage": self._stage(row),
                    "source_kind": _code(row.get("source_kind")),
                    "progress_percent": self._progress(row),
                    "bytes_done": max(0, int(row.get("bytes_done") or 0)),
                    "bytes_total": max(0, int(row.get("bytes_total") or 0)),
                    "current_item": max(0, int(row.get("current_item") or 0)),
                    "total_items": total_items,
                    "backup_state": _code(row.get("backup_state"), fallback="disabled"),
                    "error_code": (
                        _code(row.get("error_code")) if row.get("error_code") else None
                    ),
                    "revision": max(1, int(row.get("revision") or 1)),
                    "accepted_at": float(row.get("accepted_at") or 0.0),
                    "updated_at": float(row.get("updated_at") or 0.0),
                    "finished_at": (
                        float(row["finished_at"]) if row.get("finished_at") is not None else None
                    ),
                }
            )
        return {
            "schema_version": self.schema_version,
            "generated_at": time.time(),
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, (total + page_size - 1) // page_size),
            "filter": filter_name,
            "items": items,
        }

    @staticmethod
    def _stats_dto(snapshot: RuntimeStatsSnapshot) -> dict[str, Any]:
        return {
            "today": {
                "accepted": snapshot.today_accepted,
                "succeeded": snapshot.today_succeeded,
                "failed": snapshot.today_failed,
                "cancelled": snapshot.today_cancelled,
            },
            "totals": {
                "published_bytes": snapshot.total_published_bytes,
                "backed_up_bytes": snapshot.total_backed_up_bytes,
                "saved_upload_bytes": snapshot.saved_upload_bytes,
            },
            "queue": {
                "running": snapshot.running,
                "waiting": snapshot.waiting,
                "failed": snapshot.failed,
            },
            "process": {
                "uptime_seconds": round(snapshot.uptime_seconds, 3),
                "cpu_percent": snapshot.cpu_percent,
                "memory_bytes": int(snapshot.memory_mb * 1024 * 1024),
            },
        }

    async def overview(self) -> dict[str, Any]:
        snapshot = await self._runtime_snapshot()
        counts = await self._repository.notification_outbox_counts()
        return {
            "schema_version": self.schema_version,
            "generated_at": time.time(),
            "stats": self._stats_dto(snapshot),
            "services": {
                "telegram_connected": snapshot.telegram_connected,
                "database_ok": snapshot.database_ok,
                "webdav_enabled": snapshot.webdav_enabled,
                "webdav_health": self._webdav_health_code(
                    snapshot.webdav_enabled, snapshot.webdav_health
                ),
                "disk_healthy": snapshot.disk_healthy,
            },
            "recent_errors": [
                {"code": _code(code), "count": max(0, int(count))}
                for code, count in snapshot.recent_errors
            ],
            "recent_events": [
                {"type": _code(event), "count": max(0, int(count))}
                for event, count in snapshot.recent_events
            ],
            "notification_outbox": {
                state: max(0, int(counts.get(state, 0)))
                for state in ("pending", "delivering", "retry_wait", "sent", "dead")
            },
        }

    async def storage(self) -> dict[str, Any]:
        snapshot = await self._runtime_snapshot()
        disk = getattr(self._pipeline, "disk", None)
        return {
            "schema_version": self.schema_version,
            "generated_at": time.time(),
            "disk": {
                "total_bytes": snapshot.disk_total_bytes,
                "used_bytes": snapshot.disk_used_bytes,
                "free_bytes": snapshot.disk_free_bytes,
                "reserved_bytes": snapshot.disk_reserved_bytes,
                "protected_cache_bytes": snapshot.protected_cache_bytes,
                "reclaimable_cache_bytes": snapshot.reclaimable_cache_bytes,
                "enforce": snapshot.disk_enforce,
                "healthy": snapshot.disk_healthy,
                "min_free_bytes": max(0, int(getattr(disk, "min_free_bytes", 0))),
                "min_free_percent": max(0.0, float(getattr(disk, "min_free_percent", 0.0))),
            },
            "backup": {
                "enabled": snapshot.webdav_enabled,
                "health": self._webdav_health_code(
                    snapshot.webdav_enabled, snapshot.webdav_health
                ),
                "cached_age_seconds": snapshot.webdav_age_seconds,
            },
        }

    async def routing(self) -> dict[str, Any]:
        destinations = []
        manager = getattr(self._pipeline, "destination_profiles", None)
        if manager is not None:
            for profile in await manager.list_profiles():
                destinations.append(
                    {
                        "id": int(profile.id),
                        "name": _label(profile.name),
                        "enabled": bool(profile.enabled),
                        "default": bool(profile.is_default),
                        "verified": profile.verified_at is not None,
                        "cover_mode": bool(profile.cover_mode),
                        "forward_caption": bool(profile.forward_caption),
                        "spoiler_mode": _code(profile.default_spoiler_mode),
                        "backup_policy": _code(profile.backup_policy),
                        "read_only": bool(profile.read_only),
                    }
                )
        sources = []
        source_manager = getattr(self._pipeline, "source_profiles", None)
        if source_manager is not None:
            for profile in await source_manager.list_profiles():
                sources.append(
                    {
                        "id": int(profile.id),
                        "name": _label(profile.name),
                        "destination_profile_id": int(profile.destination_profile_id),
                        "enabled": bool(profile.enabled),
                        "verified": profile.verified_at is not None,
                        "spoiler_policy": _code(profile.spoiler_policy),
                        "caption_policy": _code(profile.caption_policy),
                        "backup_policy": _code(profile.backup_policy),
                    }
                )
        return {
            "schema_version": self.schema_version,
            "generated_at": time.time(),
            "destinations": destinations,
            "sources": sources,
        }

    async def health(self) -> dict[str, Any]:
        snapshot = await self._runtime_snapshot()
        live, ready, age = self._stats.heartbeat_snapshot()
        try:
            schema_versions = await self._repository.schema_versions()
        except Exception:
            schema_versions = []
        return {
            "schema_version": self.schema_version,
            "generated_at": time.time(),
            "live": live,
            "ready": ready,
            "heartbeat_age_seconds": age,
            "database_ok": snapshot.database_ok,
            "schema_versions": schema_versions,
            "telegram_connected": snapshot.telegram_connected,
            "webdav_enabled": snapshot.webdav_enabled,
            "webdav_health": self._webdav_health_code(
                snapshot.webdav_enabled, snapshot.webdav_health
            ),
            "disk_healthy": snapshot.disk_healthy,
            "app_commit": _code(os.environ.get("APP_COMMIT"), fallback="unknown", limit=40),
        }

    async def metrics(self) -> str:
        snapshot = await self._runtime_snapshot()
        live, ready, _age = self._stats.heartbeat_snapshot()
        outbox = await self._repository.notification_outbox_counts()
        values = {
            "tvf_up": int(live),
            "tvf_ready": int(ready),
            "tvf_telegram_connected": int(snapshot.telegram_connected),
            "tvf_database_ok": int(snapshot.database_ok),
            "tvf_webdav_enabled": int(snapshot.webdav_enabled),
            "tvf_webdav_healthy": int(
                self._webdav_health_code(snapshot.webdav_enabled, snapshot.webdav_health)
                == "normal"
            ),
            "tvf_disk_healthy": int(bool(snapshot.disk_healthy)),
            "tvf_disk_enforced": int(snapshot.disk_enforce),
            "tvf_process_uptime_seconds": snapshot.uptime_seconds,
            "tvf_process_memory_bytes": int(snapshot.memory_mb * 1024 * 1024),
            "tvf_jobs_running": snapshot.running,
            "tvf_jobs_waiting": snapshot.waiting,
            "tvf_jobs_failed": snapshot.failed,
            "tvf_today_accepted_jobs": snapshot.today_accepted,
            "tvf_today_succeeded_jobs": snapshot.today_succeeded,
            "tvf_today_failed_jobs": snapshot.today_failed,
            "tvf_today_cancelled_jobs": snapshot.today_cancelled,
            "tvf_published_bytes_total": snapshot.total_published_bytes,
            "tvf_backed_up_bytes_total": snapshot.total_backed_up_bytes,
            "tvf_saved_upload_bytes_total": snapshot.saved_upload_bytes,
            "tvf_disk_free_bytes": snapshot.disk_free_bytes or 0,
            "tvf_disk_reserved_bytes": snapshot.disk_reserved_bytes,
        }
        lines = [
            "# HELP tvf_up Local heartbeat liveness.",
            "# TYPE tvf_up gauge",
        ]
        for name, value in values.items():
            lines.append(f"{name} {float(value):.6g}")
        lines.extend(
            [
                "# HELP tvf_notification_outbox Number of notification records by bounded state.",
                "# TYPE tvf_notification_outbox gauge",
            ]
        )
        for state in ("pending", "delivering", "retry_wait", "sent", "dead"):
            lines.append(f'tvf_notification_outbox{{state="{state}"}} {int(outbox.get(state, 0))}')
        return "\n".join(lines) + "\n"
