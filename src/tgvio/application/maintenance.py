from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Callable
from uuid import uuid4

from tgvio.application.auto_recovery import (
    archive_failure_waits_for_recovery,
    job_failure_waits_for_recovery,
)
from tgvio.application.ports import CacheOperator, JobRepository, PublishedMessageRemover
from tgvio.application.runtime_flags import RuntimeFlags
from tgvio.domain.archive import ArchiveDeletionState, ArchivePackageState, ArchivePolicy
from tgvio.domain.job import Job, JobState
from tgvio.domain.maintenance import (
    BusinessDay,
    VisibilityReason,
    business_day_bounds,
)
from tgvio.observability import log_event


_ROTATED_LOG_GLOB = "tgvio.jsonl.*"
_TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
_ARCHIVE_ACTIVE = {
    ArchivePackageState.PLANNED,
    ArchivePackageState.STAGING,
    ArchivePackageState.UPLOADING,
    ArchivePackageState.VERIFYING,
}
_STATUS_DELETE_TIMEOUT_SECONDS = 10.0
_RUN_KIND = "daily_hide"


class HistoryMaintenanceService:
    """Safely hide settled Jobs and reclaim only safe cache/records.

    It never deletes Job, publish, Archive, dedup or audit history. Only the
    default list visibility, the frozen old status messages, the managed local
    cache for hidden Jobs, and expired/consumed operation tokens are touched.
    """

    def __init__(
        self,
        repository: JobRepository,
        *,
        cache_operator: CacheOperator | None = None,
        status_cleaner: PublishedMessageRemover | None = None,
        log_dir: Path | None = None,
        log_retention_days: int = 3,
        retention_hours: int = 24,
        hour: int = 6,
        candidate_batch: int = 200,
        holder_id: str | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._repository = repository
        self._cache_operator = cache_operator
        self._status_cleaner = status_cleaner
        self._log_dir = log_dir
        self._log_retention_days = max(1, int(log_retention_days))
        self._retention_hours = max(1, int(retention_hours))
        self._hour = int(hour)
        self._candidate_batch = max(1, int(candidate_batch))
        self._holder_id = holder_id or uuid4().hex
        self._now = now or time.time
        self._log = logging.getLogger("tgvio.maintenance")

    def set_hour(self, hour: int) -> None:
        self._hour = max(0, min(23, int(hour)))

    async def run(self) -> dict[str, object]:
        now = float(self._now())
        bounds = business_day_bounds(now, hour=self._hour)
        existing = await self._repository.get_maintenance_run(
            kind=_RUN_KIND,
            business_day=bounds.day,
        )
        if existing is not None and str(existing["status"]) == "completed":
            return {"business_day": bounds.day, "status": "completed", "skipped": True}
        run = await self._repository.ensure_maintenance_run(
            kind=_RUN_KIND,
            business_day=bounds.day,
            cutoff_at=bounds.start_epoch,
            now=now,
        )
        claimed = await self._repository.claim_maintenance_run(
            int(run["id"]),
            holder_id=self._holder_id,
            now=now,
            lease_seconds=300.0,
        )
        if claimed is None:
            return {"business_day": bounds.day, "status": "contended"}
        try:
            frozen = 0
            if not await self._repository.has_maintenance_targets(int(run["id"])):
                frozen = await self._freeze_candidates(claimed, bounds, now)
            result = await self._process(claimed, bounds, now)
            logs_removed = self._prune_logs()
            await self._repository.finish_maintenance_run(
                int(run["id"]), status="completed", now=now
            )
            result.update(
                {
                    "business_day": bounds.day,
                    "status": "completed",
                    "frozen": frozen,
                    "logs_removed": logs_removed,
                }
            )
            log_event(
                self._log,
                logging.INFO,
                "maintenance.daily.completed",
                business_day=bounds.day,
                **{key: value for key, value in result.items() if key != "business_day"},
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt = int(claimed.get("attempt") or 0) + 1
            if attempt >= 3:
                await self._repository.finish_maintenance_run(
                    int(run["id"]), status="failed", now=now, error=type(exc).__name__, attempt=attempt
                )
            else:
                delay = (30, 120, 600)[min(attempt, 2) - 1]
                await self._repository.finish_maintenance_run(
                    int(run["id"]),
                    status="retry_wait",
                    now=now,
                    error=type(exc).__name__,
                    next_retry_at=now + delay,
                    attempt=attempt,
                )
            log_event(
                self._log,
                logging.ERROR,
                "maintenance.daily.failed",
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            raise

    async def _freeze_candidates(
        self,
        run: dict[str, object],
        bounds: BusinessDay,
        now: float,
    ) -> int:
        run_id = int(run["id"])
        candidates: list[Job] = []
        offset = 0
        while True:
            ids = await self._repository.list_terminal_job_ids_before(
                cutoff_epoch=bounds.start_epoch,
                limit=self._candidate_batch,
                offset=offset,
            )
            if not ids:
                break
            for job_id in ids:
                job = await self._repository.get(job_id)
                if job is None:
                    continue
                if await self._is_eligible(job, now):
                    candidates.append(job)
            offset += len(ids)
        targets: list[dict[str, object]] = []
        for job in candidates:
            ref = await self._repository.get_job_display_message(job.id)
            targets.append(
                {
                    "job_id": job.id,
                    "chat_id": None if ref is None else int(ref.chat_id),
                    "message_id": None if ref is None else int(ref.message_id),
                }
            )
        return await self._repository.upsert_maintenance_targets(
            run_id, tuple(targets), now=now
        )

    async def _process(
        self,
        run: dict[str, object],
        bounds: BusinessDay,
        now: float,
    ) -> dict[str, object]:
        run_id = int(run["id"])
        hidden_ids: list[str] = []
        status_deleted = 0
        skipped = 0
        targets = await self._repository.list_unfinished_maintenance_targets(run_id)
        for target in targets:
            job_id = str(target["job_id"])
            job = await self._repository.get(job_id)
            if job is None or not await self._is_eligible(job, now):
                await self._repository.set_maintenance_target_status(
                    int(target["id"]), status="skipped", now=now, phase="hide"
                )
                skipped += 1
                continue
            await self._repository.hide_jobs(
                (job_id,), reason=VisibilityReason.DAILY_ROLLOVER.value, now=now
            )
            hidden_ids.append(job_id)
            chat_id = target["display_chat_id"]
            message_id = target["display_message_id"]
            if (
                self._status_cleaner is not None
                and chat_id is not None
                and message_id is not None
                and await self._delete_status_bounded(int(chat_id), int(message_id))
            ):
                status_deleted += 1
            await self._repository.set_maintenance_target_status(
                int(target["id"]), status="done", now=now, phase="done"
            )

        cache_removed = 0
        if self._cache_operator is not None and hidden_ids:
            try:
                result = await self._cache_operator.cleanup(
                    force=True, job_ids=tuple(hidden_ids)
                )
                cache_removed = int(getattr(result, "removed_jobs", 0) or 0)
            except Exception as exc:
                log_event(
                    self._log,
                    logging.WARNING,
                    "maintenance.cache.failed",
                    exception_type=type(exc).__name__,
                )

        token_cutoff = now - self._retention_hours * 3600
        tokens_recycled = await self._repository.recycle_operation_tokens(
            now=now, consumed_grace_seconds=self._retention_hours * 3600
        )
        outbox_pruned = await self._repository.prune_settled_outbox(
            cutoff_epoch=token_cutoff
        )
        return {
            "hidden": len(hidden_ids),
            "skipped": skipped,
            "status_messages_deleted": status_deleted,
            "cache_removed": cache_removed,
            "tokens_recycled": tokens_recycled,
            "outbox_pruned": outbox_pruned,
        }

    async def _is_eligible(self, job: Job, now: float) -> bool:
        if job.state not in _TERMINAL_STATES:
            return False
        if job.error_code in {"publish_partial", "publish_uncertain"}:
            return False
        if job_failure_waits_for_recovery(job):
            return False
        if await self._repository.job_has_active_claim(job.id, now=now):
            return False
        if await self._repository.job_has_unsettled_publish_step(job.id):
            return False
        if await self._repository.job_has_pending_revocation(job.id):
            return False
        package = await self._repository.get_archive_package_for_job(job.id)
        if package is not None:
            if package.state in _ARCHIVE_ACTIVE:
                return False
            if package.state == ArchivePackageState.FAILED:
                if package.archive_policy == ArchivePolicy.REQUIRED:
                    return False
                if archive_failure_waits_for_recovery(job, package):
                    return False
            deletion = await self._repository.get_archive_deletion(package.id)
            if deletion is not None and deletion.state != ArchiveDeletionState.DELETED:
                return False
        return True

    async def _delete_status_bounded(self, chat_id: int, message_id: int) -> bool:
        if self._status_cleaner is None:
            return False
        try:
            await asyncio.wait_for(
                self._status_cleaner.delete_message(chat_id, message_id),
                timeout=_STATUS_DELETE_TIMEOUT_SECONDS,
            )
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return False
        except Exception:
            # A message that no longer exists is an acceptable outcome.
            return False

    def _prune_logs(self) -> int:
        if self._log_dir is None or not self._log_dir.is_dir():
            return 0
        cutoff = time.time() - self._log_retention_days * 86400
        removed = 0
        for path in self._log_dir.glob(_ROTATED_LOG_GLOB):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


class HistoryMaintenanceRuntime:
    """Durably trigger the daily safe-hide maintenance at a Beijing wall clock."""

    def __init__(
        self,
        service: HistoryMaintenanceService,
        flags: RuntimeFlags,
        *,
        poll_seconds: float = 60.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._service = service
        self._flags = flags
        self._poll_seconds = max(5.0, float(poll_seconds))
        self._now = now or time.time
        self._task: asyncio.Task | None = None
        self._log = logging.getLogger("tgvio.maintenance.runtime")

    async def start(self) -> None:
        if self._task is not None:
            return
        await self._tick()
        self._task = asyncio.create_task(self._run(), name="tgvio-daily-maintenance")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _tick(self) -> None:
        if not self._flags.bool("daily_cleanup_enabled", True):
            return
        try:
            raw = str(self._flags.get("daily_cleanup_time", "06:00") or "06:00")
            self._service.set_hour(int(raw.split(":")[0]))
        except (TypeError, ValueError):
            self._service.set_hour(6)
        try:
            await self._service.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                self._log,
                logging.ERROR,
                "maintenance.runtime.failed",
                exception_type=type(exc).__name__,
                exc_info=True,
            )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            await self._tick()
