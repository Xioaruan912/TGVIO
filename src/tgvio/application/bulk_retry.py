from __future__ import annotations

from dataclasses import dataclass, field
import logging

from tgvio.application.item_recovery import (
    SkippedItemRecoveryService,
    SkippedItemRecoveryUnavailableError,
)
from tgvio.application.job_control import JobControlService, RetryDecision, UnsafeRetryError
from tgvio.application.ports import ArchiveOperator, JobRepository
from tgvio.domain.job import DOWNLOAD_SKIPPED_CODE_KEY, Job, item_download_skipped
from tgvio.observability import log_event


# Possibly-visible publishes must be checked by a person; deleted sources can
# never succeed. Neither is touched by "retry all".
NOT_RETRYABLE_CODES = frozenset({"publish_partial", "publish_uncertain", "source_missing"})
_FAILURE_PAGE_SIZE = 20
_MAX_FAILURE_PAGES = 5


@dataclass(frozen=True, slots=True)
class BulkRetryPlan:
    """What one "retry all" would do, computed from durable state only."""

    job_ids: tuple[str, ...] = ()
    archive_job_ids: tuple[str, ...] = ()
    skipped_parent_ids: tuple[str, ...] = ()
    skipped_items: int = 0
    blocked_job_ids: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.job_ids or self.archive_job_ids or self.skipped_parent_ids)

    def payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "jobs": list(self.job_ids),
            "archives": list(self.archive_job_ids),
            "skipped": list(self.skipped_parent_ids),
        }


@dataclass(slots=True)
class BulkRetryResult:
    retried: list[RetryDecision] = field(default_factory=list)
    recovery_children: list[Job] = field(default_factory=list)
    archives_requeued: int = 0
    unchanged: int = 0


class BulkRetryService:
    """Run every safe retry an owner could start by hand, in one operation.

    Each action is the same idempotent operation the per-job buttons use, so a
    retry that became unsafe meanwhile is refused exactly as it would be there.
    """

    def __init__(
        self,
        repository: JobRepository,
        control: JobControlService,
        *,
        item_recovery: SkippedItemRecoveryService | None = None,
        archive_operator: ArchiveOperator | None = None,
    ) -> None:
        self._repository = repository
        self._control = control
        self._item_recovery = item_recovery
        self._archive_operator = archive_operator
        self._log = logging.getLogger("tgvio.bulk_retry")

    async def plan(self, owner_id: int) -> BulkRetryPlan:
        job_ids: list[str] = []
        archive_ids: list[str] = []
        blocked: list[str] = []
        for page in range(_MAX_FAILURE_PAGES):
            result = await self._repository.page_failures(
                owner_id=owner_id, page=page, page_size=_FAILURE_PAGE_SIZE
            )
            for entry in result.entries:
                if entry.job_actionable:
                    if entry.job.error_code in NOT_RETRYABLE_CODES:
                        blocked.append(entry.job.id)
                    else:
                        job_ids.append(entry.job.id)
                if entry.archive_actionable and self._archive_operator is not None:
                    archive_ids.append(entry.job.id)
            if result.page + 1 >= result.total_pages:
                break

        parent_ids: list[str] = []
        skipped_items = 0
        if self._item_recovery is not None:
            parents = await self._repository.list_unrecovered_skipped_parents(owner_id=owner_id)
            for parent in parents:
                recoverable = recoverable_skipped_items(parent)
                if recoverable:
                    parent_ids.append(parent.id)
                    skipped_items += recoverable
        return BulkRetryPlan(
            job_ids=tuple(job_ids),
            archive_job_ids=tuple(archive_ids),
            skipped_parent_ids=tuple(parent_ids),
            skipped_items=skipped_items,
            blocked_job_ids=tuple(blocked),
        )

    async def run(self, owner_id: int, plan: BulkRetryPlan) -> BulkRetryResult:
        result = BulkRetryResult()
        for job_id in plan.job_ids:
            job = await self._owned(owner_id, job_id)
            try:
                if job is None:
                    raise ValueError("job unavailable")
                result.retried.append(await self._control.retry_failed(job))
            except (ValueError, UnsafeRetryError):
                result.unchanged += 1
        for job_id in plan.archive_job_ids:
            package = await self._repository.get_archive_package_for_job(job_id)
            try:
                if package is None or self._archive_operator is None:
                    raise ValueError("archive unavailable")
                await self._archive_operator.retry_package(package.id)
                result.archives_requeued += 1
            except (KeyError, ValueError, RuntimeError):
                result.unchanged += 1
        for job_id in plan.skipped_parent_ids:
            try:
                if self._item_recovery is None:
                    raise SkippedItemRecoveryUnavailableError("recovery disabled")
                child = await self._item_recovery.recover(parent_job_id=job_id, owner_id=owner_id)
                result.recovery_children.append(child)
            except SkippedItemRecoveryUnavailableError:
                result.unchanged += 1
        log_event(
            self._log,
            logging.INFO,
            "bulk_retry.completed",
            "Owner retried every safe failure at once",
            owner_id=owner_id,
            jobs=len(result.retried),
            archives=result.archives_requeued,
            recovery_jobs=len(result.recovery_children),
            unchanged=result.unchanged,
        )
        return result

    async def _owned(self, owner_id: int, job_id: str) -> Job | None:
        job = await self._repository.get(job_id)
        if job is None or int(job.owner_id) != int(owner_id):
            return None
        return job


def recoverable_skipped_items(job: Job) -> int:
    """Skipped media that a recovery Job could still fetch (not deleted at source)."""

    return sum(
        1
        for item in job.items
        if item_download_skipped(item)
        and item.metadata.get(DOWNLOAD_SKIPPED_CODE_KEY) != "source_missing"
    )
