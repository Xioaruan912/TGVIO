from __future__ import annotations

import resource
import time

from tgvio.application.diagnostics import DiagnosticSnapshotService
from tgvio.application.ports import JobRepository
from tgvio.domain.diagnostics import DiagnosticAvailability


_JOB_STATES = (
    "received",
    "downloading",
    "downloaded",
    "analyzing",
    "analyzed",
    "planned",
    "publishing",
    "succeeded",
    "failed",
    "cancelled",
)
_OUTBOX_STATES = ("pending", "claimed", "sent", "dead")
_ARCHIVE_STATES = ("planned", "staging", "uploading", "verifying", "committed", "failed", "cancelled")


class MetricsService:
    """Low-cardinality Prometheus exposition with fixed label allowlists.

    Job ids, user ids, URLs and error text never become labels.
    """

    def __init__(
        self,
        repository: JobRepository,
        diagnostic_service: DiagnosticSnapshotService,
        *,
        started_at: float | None = None,
    ) -> None:
        self._repository = repository
        self._diagnostics = diagnostic_service
        self._started_at = float(started_at if started_at is not None else time.time())

    async def render(self) -> str:
        snapshot = await self._diagnostics.snapshot()
        counts = await self._repository.count_by_state()
        counts = {str(getattr(state, "value", state)): int(value) for state, value in counts.items()}
        try:
            aggregates = await self._repository.get_diagnostic_aggregates()
        except Exception:
            aggregates = {}
        try:
            outbox = await self._repository.count_notification_outbox()
        except Exception:
            outbox = {}
        runtime = await self._repository.get_runtime_health()

        lines: list[str] = []

        def gauge(name: str, value: object, help_text: str = "") -> None:
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")

        gauge("tgvio_ready", 1 if snapshot.aggregate_status == DiagnosticAvailability.READY else 0)
        gauge("tgvio_schema_user_version", snapshot.schema.user_version or 0)
        gauge("tgvio_runtime_lease_unique", 1 if snapshot.runtime_lease.unique else 0)
        gauge("tgvio_scheduler_paused", 1 if aggregates.get("queue_paused") in (True, 1) else 0)
        lines.append("# HELP tgvio_jobs Job count by terminal/queue state")
        lines.append("# TYPE tgvio_jobs gauge")
        for state in _JOB_STATES:
            lines.append(f'tgvio_jobs{{state="{state}"}} {counts.get(state, 0)}')
        lines.append("# HELP tgvio_archive_packages Archive package count by state")
        lines.append("# TYPE tgvio_archive_packages gauge")
        for state in _ARCHIVE_STATES:
            lines.append(
                f'tgvio_archive_packages{{state="{state}"}} '
                f'{_archive_count(snapshot, state)}'
            )
        lines.append("# HELP tgvio_notification_outbox Notification outbox count by state")
        lines.append("# TYPE tgvio_notification_outbox gauge")
        for state in _OUTBOX_STATES:
            lines.append(
                f'tgvio_notification_outbox{{state="{state}"}} {int(outbox.get(state, 0))}'
            )
        for component in ("runtime", "telegram", "schema", "static_proxy"):
            entry = runtime.get(component)
            status = str(entry.get("status", "unknown")) if isinstance(entry, dict) else "unknown"
            lines.append(
                f'tgvio_component_up{{component="{component}",status="{_safe_label(status)}"}} '
                f'{1 if status in {"alive", "connected", "ready", "reachable"} else 0}'
            )
        gauge("tgvio_uptime_seconds", int(max(0.0, time.time() - self._started_at)))
        gauge("tgvio_process_resident_memory_bytes", _resident_memory_bytes())
        return "\n".join(lines) + "\n"


def _archive_count(snapshot, state: str) -> int:
    return {
        "planned": snapshot.archive.planned,
        "committed": snapshot.archive.committed,
        "failed": snapshot.archive.failed,
        "staging": snapshot.archive.transferring,
        "uploading": snapshot.archive.transferring,
        "verifying": snapshot.archive.transferring,
    }.get(state, 0)


def _safe_label(value: str) -> str:
    return "".join(character for character in value if character.isalnum() or character == "_")[:32] or "unknown"


def _resident_memory_bytes() -> int:
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
    except Exception:
        return 0
    divisor = 1024 if "linux" not in _platform() else 1
    return int(max(0, usage.ru_maxrss) * divisor)


def _platform() -> str:
    import sys

    return sys.platform
