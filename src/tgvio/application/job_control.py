from __future__ import annotations

from dataclasses import dataclass

from tgvio.application.ports import JobRepository
from tgvio.domain.control import JobControlState, QueueControlState
from tgvio.domain.job import Job, JobState
from tgvio.domain.publish import PublishStepState
from tgvio.domain.progress import JobProgress


class JobCancelRequested(RuntimeError):
    pass


class UnsafeRetryError(RuntimeError):
    pass


class JobHoldRequested(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RetryDecision:
    job: Job
    target_state: JobState
    retry_count: int


class JobControlService:
    """Durable operator controls with side-effect-aware retry rules."""

    _PREPUBLISH_FAILURE_STATES = {
        JobState.RECEIVED,
        JobState.DOWNLOADING,
        JobState.DOWNLOADED,
        JobState.ANALYZING,
        JobState.ANALYZED,
    }

    _BLOCKED_PUBLISH_CODES = {"publish_partial", "publish_uncertain"}

    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    async def request_cancel(self, job: Job, *, reason: str | None = None) -> Job:
        if job.state in {JobState.SUCCEEDED, JobState.CANCELLED}:
            raise ValueError(f"job cannot be cancelled from {job.state.value}")
        if job.state == JobState.FAILED:
            raise ValueError("failed jobs are not active; use retry or leave them failed")
        await self._repository.request_cancel(job.id, reason=reason)
        current = await self._repository.get(job.id)
        if current is None:
            raise KeyError(f"job not found after cancel request: {job.id}")
        if current.state == JobState.PLANNED:
            try:
                cancelled = await self._repository.transition(
                    job.id,
                    JobState.CANCELLED,
                    event_type="cancelled",
                    detail=reason or "cancel requested before publish",
                )
                await self._repository.set_job_progress(
                    JobProgress(
                        job_id=job.id,
                        phase="cancelled",
                        detail_code="cancel_requested",
                        item_total=len(job.items),
                    )
                )
                return cancelled
            except ValueError:
                # Publishing may have started between the read and transition.
                # The durable cancel flag remains set and will be honored at
                # the next execution checkpoint.
                refreshed = await self._repository.get(job.id)
                if refreshed is None:
                    raise KeyError(f"job not found after cancel race: {job.id}")
                return refreshed
        return current

    async def request_hold(self, job: Job, *, reason: str | None = None) -> JobControlState:
        if job.terminal:
            raise ValueError(f"job cannot be held from {job.state.value}")
        return await self._repository.request_hold(job.id, reason=reason)

    async def resume(self, job: Job) -> JobControlState:
        if job.terminal:
            raise ValueError(f"job cannot be resumed from {job.state.value}")
        return await self._repository.clear_hold(job.id)

    async def pause_queue(self, *, reason: str | None = None) -> QueueControlState:
        return await self._repository.set_queue_paused(True, reason=reason)

    async def resume_queue(self) -> QueueControlState:
        return await self._repository.set_queue_paused(False)

    async def safe_checkpoint(self, job: Job, *, detail: str) -> None:
        await self.checkpoint(job, detail=detail)
        control = await self._repository.get_job_control(job.id)
        if control.hold_requested:
            raise JobHoldRequested(detail)

    async def checkpoint(self, job: Job, *, detail: str) -> None:
        if not await self._repository.is_cancel_requested(job.id):
            return
        if job.state == JobState.PLANNED:
            await self._repository.transition(
                job.id,
                JobState.CANCELLED,
                event_type="cancelled",
                detail=detail,
            )
        elif job.state in {
            JobState.RECEIVED,
            JobState.DOWNLOADING,
            JobState.DOWNLOADED,
            JobState.ANALYZING,
            JobState.ANALYZED,
            JobState.PUBLISHING,
        }:
            await self._repository.transition(
                job.id,
                JobState.CANCELLED,
                event_type="cancelled",
                detail=detail,
            )
        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="cancelled",
                detail_code="cancel_requested",
                item_total=len(job.items),
            )
        )
        raise JobCancelRequested(detail)

    async def retry_failed(self, job: Job) -> RetryDecision:
        if job.state != JobState.FAILED:
            raise ValueError(f"job must be failed before retry: {job.state.value}")
        if job.error_code in self._BLOCKED_PUBLISH_CODES:
            raise UnsafeRetryError(
                f"{job.error_code} requires manual side-effect review before retry"
            )

        events = await self._repository.list_events(job.id)
        failed_event = next(
            (event for event in reversed(events) if event.to_state == JobState.FAILED),
            None,
        )
        origin = failed_event.from_state if failed_event is not None else None

        if origin in self._PREPUBLISH_FAILURE_STATES:
            target = JobState.RECEIVED
        elif origin == JobState.PUBLISHING:
            plan = await self._repository.get_publish_plan(job.id)
            if plan is None:
                raise UnsafeRetryError("failed publish job has no durable PublishPlan")
            effects = await self._repository.list_publish_effects(plan.id)
            failed_steps = [step for step in plan.steps if step.state == PublishStepState.FAILED]
            if len(failed_steps) != 1:
                raise UnsafeRetryError("publish retry requires exactly one failed step")
            failed_step = failed_steps[0]
            visible = [
                effect
                for effect in effects
                if effect.step_index == failed_step.index
                and effect.effect_type != "publish_step_receipts_committed"
            ]
            if visible:
                raise UnsafeRetryError(
                    "failed publish step already has confirmed side effects; refusing blind retry"
                )
            await self._repository.update_publish_step_state(
                plan.id,
                failed_step.index,
                PublishStepState.PENDING,
            )
            target = JobState.PLANNED
        else:
            raise UnsafeRetryError(
                f"cannot derive a safe retry target from failure origin {origin}"
            )

        await self._repository.clear_cancel_request(job.id)
        retry_count = await self._repository.increment_retry_count(job.id)
        retried = await self._repository.transition(
            job.id,
            target,
            event_type="retry_requested",
            detail=f"retry_count={retry_count};from={origin.value if origin else 'unknown'}",
        )
        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="queued" if target == JobState.RECEIVED else "planned",
                current=0,
                total=0,
                detail_code=f"retry_{retry_count}",
                item_total=len(job.items),
            )
        )
        return RetryDecision(retried, target, retry_count)
