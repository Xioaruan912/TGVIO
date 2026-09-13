from __future__ import annotations

from typing import Awaitable, Callable

from tgvio.application.diagnostics import DiagnosticSnapshotService
from tgvio.application.ports import JobRepository
from tgvio.domain.diagnostics import DiagnosticSnapshot


_JOB_FILTER_STATES: dict[str, tuple[str, ...]] = {
    "all": (),
    "active": (
        "received",
        "downloading",
        "downloaded",
        "analyzing",
        "analyzed",
        "planned",
        "publishing",
    ),
    "failed": ("failed",),
    "completed": ("succeeded", "cancelled"),
}

_ALLOWED_JOB_FIELDS = (
    "job_id",
    "state",
    "error_code",
    "updated_at",
    "media_count",
    "bytes",
    "accepted_order",
)


class DashboardService:
    """Assemble read-only, redacted DTOs for the private operations surface.

    The service performs no Telegram/WebDAV/network calls, never selects owner,
    peer, caption, URL or path fields, and never writes business state.
    """

    def __init__(
        self,
        repository: JobRepository,
        diagnostic_service: DiagnosticSnapshotService,
        *,
        disk_usage: Callable[[], dict[str, int] | None] | None = None,
    ) -> None:
        self._repository = repository
        self._diagnostics = diagnostic_service
        self._disk_usage = disk_usage

    async def overview(self) -> dict[str, object]:
        snapshot = await self._snapshot()
        counts = await self._counts()
        stats = await self._repository.get_stats_snapshot()
        aggregates = await self._safe_aggregates()
        outbox = await self._safe_outbox()
        return {
            "release": _release(snapshot),
            "jobs": counts,
            "stats": {str(key): int(value) for key, value in stats.items()},
            "scheduler": _scheduler(aggregates),
            "archive": _archive(snapshot),
            "outbox": outbox,
            "features": _features(snapshot),
            "static_proxy": _static_proxy(snapshot),
            "disk": self._disk(),
        }

    async def jobs(
        self,
        *,
        filter: str = "all",
        page: int = 0,
        page_size: int = 20,
    ) -> dict[str, object]:
        states = _JOB_FILTER_STATES.get(filter, _JOB_FILTER_STATES["all"])
        page_data = await self._repository.get_admin_job_page(
            states=states,
            page=page,
            page_size=page_size,
        )
        entries = [
            {key: row.get(key) for key in _ALLOWED_JOB_FIELDS} for row in page_data["entries"]
        ]
        return {
            "filter": filter if filter in _JOB_FILTER_STATES else "all",
            "page": int(page_data["page"]),
            "page_size": int(page_data["page_size"]),
            "total": int(page_data["total"]),
            "entries": entries,
        }

    async def routing(self) -> dict[str, object]:
        snapshot = await self._snapshot()
        return {
            "features": _features(snapshot),
            "archive": _archive(snapshot),
            "static_proxy": _static_proxy(snapshot),
        }

    async def storage(self) -> dict[str, object]:
        return {"disk": self._disk()}

    async def health(self) -> dict[str, object]:
        snapshot = await self._snapshot()
        aggregates = await self._safe_aggregates()
        runtime = await self._repository.get_runtime_health()
        return {
            "release": _release(snapshot),
            "schema": {
                "user_version": snapshot.schema.user_version,
                "latest_version": snapshot.schema.latest_version,
                "ledger_contiguous": snapshot.schema.ledger_contiguous,
                "verification": snapshot.schema.verification.value,
            },
            "runtime_lease": {
                "unique": snapshot.runtime_lease.unique,
                "generation": snapshot.runtime_lease.generation,
                "freshness": snapshot.runtime_lease.freshness.value,
            },
            "runtime": {
                str(component): str(entry.get("status", "unknown"))
                for component, entry in runtime.items()
                if isinstance(entry, dict)
            },
            "scheduler": _scheduler(aggregates),
            "aggregate_status": snapshot.aggregate_status.value,
        }

    async def _snapshot(self) -> DiagnosticSnapshot:
        return await self._diagnostics.snapshot()

    async def _counts(self) -> dict[str, int]:
        raw = await self._repository.count_by_state()
        return {str(getattr(state, "value", state)): int(count) for state, count in raw.items()}

    async def _safe_aggregates(self) -> dict[str, object]:
        try:
            return await self._repository.get_diagnostic_aggregates()
        except Exception:
            return {}

    async def _safe_outbox(self) -> dict[str, int]:
        try:
            return await self._repository.count_notification_outbox()
        except Exception:
            return {}

    def _disk(self) -> dict[str, int] | None:
        if self._disk_usage is None:
            return None
        try:
            return self._disk_usage()
        except Exception:
            return None


def _release(snapshot: DiagnosticSnapshot) -> dict[str, str]:
    return {
        "release_id": snapshot.release_id,
        "app_version": snapshot.app_version,
        "commit": snapshot.commit,
        "source_manifest": snapshot.source_manifest,
    }


def _scheduler(aggregates: dict[str, object]) -> dict[str, object]:
    return {
        "paused": aggregates.get("queue_paused") in (True, 1),
        "active": _int(aggregates.get("scheduler_active")),
        "held": _int(aggregates.get("scheduler_held")),
        "ready": _int(aggregates.get("scheduler_ready")),
        "blocked": _int(aggregates.get("scheduler_blocked")),
    }


def _archive(snapshot: DiagnosticSnapshot) -> dict[str, object]:
    return {
        "enabled": snapshot.features.archive_enabled,
        "policy": snapshot.features.archive_policy,
        "planned": snapshot.archive.planned,
        "transferring": snapshot.archive.transferring,
        "committed": snapshot.archive.committed,
        "failed": snapshot.archive.failed,
        "retry_wait": snapshot.archive.retry_wait,
        "capability_freshness": snapshot.archive.capability_freshness,
    }


def _features(snapshot: DiagnosticSnapshot) -> dict[str, object]:
    return {
        "run_bot": snapshot.features.run_bot,
        "publish_enabled": snapshot.features.publish_enabled,
        "url_enabled": snapshot.features.url_enabled,
        "url_private_network_policy": snapshot.features.url_private_network_policy,
        "archive_enabled": snapshot.features.archive_enabled,
        "archive_policy": snapshot.features.archive_policy,
        "collections_enabled": snapshot.features.collections_enabled,
        "auto_retry_enabled": snapshot.features.auto_retry_enabled,
        "live_fixture_enabled": snapshot.features.live_fixture_enabled,
    }


def _static_proxy(snapshot: DiagnosticSnapshot) -> dict[str, object]:
    return {
        "state": snapshot.static_proxy.state.value,
        "checked_at_epoch": snapshot.static_proxy.checked_at_epoch,
    }


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    return 0
