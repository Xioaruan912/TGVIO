from __future__ import annotations

import logging
import time

from tgvio.application.job_control import JobCancelRequested, JobControlService, JobHoldRequested
from tgvio.application.ports import (
    JobRepository,
    PublishTransport,
    PublishTransportPartialError,
    PublishTransportUncertainError,
)
from tgvio.domain.job import Job, JobState
from tgvio.domain.publish import PublishEffect, PublishPlan, PublishReceipt, PublishStepState
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event


class PublishRecoveryRequired(RuntimeError):
    def __init__(self, code: str, message: str, *, step_index: int) -> None:
        super().__init__(message)
        self.code = code
        self.step_index = step_index


class IncompletePublishReceipts(RuntimeError):
    pass


class PublishExecutionEngine:
    """Side-effect journal aware executor with conservative restart recovery."""

    _COMMIT_EFFECT_TYPE = "publish_step_receipts_committed"

    def __init__(
        self,
        repository: JobRepository,
        transport: PublishTransport,
        control: JobControlService | None = None,
    ) -> None:
        self._repository = repository
        self._transport = transport
        self._control = control
        self._log = logging.getLogger("tgvio.publish.execution")

    async def execute(self, job: Job, plan: PublishPlan) -> Job:
        started_at = time.monotonic()
        log_event(
            self._log,
            logging.INFO,
            "publish.job.started",
            job_id=job.id,
            plan_id=plan.id,
            state=job.state.value,
            step_count=len(plan.steps),
            item_count=len(job.items),
        )
        await self._safe_checkpoint(job, "paused before publish started")
        if job.state == JobState.PLANNED:
            job = await self._repository.transition(
                job.id,
                JobState.PUBLISHING,
                event_type="publish_started",
                detail=f"plan={plan.id}",
            )
        elif job.state == JobState.PUBLISHING:
            await self._reconcile_inflight(job, plan)
        else:
            raise ValueError(f"job must be planned/publishing before execution: {job.state.value}")
        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="publishing",
                current=0,
                total=len(plan.steps),
                item_total=len(job.items),
            )
        )
        active_step_index: int | None = None
        active_receipts: tuple[PublishReceipt, ...] = ()
        try:
            for original_step in plan.steps:
                active_receipts = ()
                await self._safe_checkpoint(
                    job,
                    f"paused before publish step {original_step.index}",
                )
                current_plan = await self._repository.get_publish_plan(job.id)
                if current_plan is None:
                    raise RuntimeError("publish plan disappeared during execution")
                step = current_plan.steps[original_step.index]
                if step.state in {PublishStepState.SUCCEEDED, PublishStepState.SKIPPED}:
                    continue
                active_step_index = step.index
                await self._repository.set_job_progress(
                    JobProgress(
                        job_id=job.id,
                        phase="publishing",
                        current=step.index,
                        total=len(plan.steps),
                        item_index=(step.item_indexes[0] if step.item_indexes else None),
                        item_total=len(job.items),
                    )
                )
                step = await self._repository.update_publish_step_state(
                    plan.id,
                    step.index,
                    PublishStepState.RUNNING,
                )
                prior_effects = tuple(
                    effect
                    for effect in await self._repository.list_publish_effects(plan.id)
                    if effect.effect_type != self._COMMIT_EFFECT_TYPE
                )
                log_event(
                    self._log,
                    logging.INFO,
                    "publish.step.started",
                    job_id=job.id,
                    plan_id=plan.id,
                    step_index=step.index,
                    step_kind=step.kind.value,
                    target=step.target.value,
                    item_count=len(step.item_indexes),
                )
                try:
                    receipts = await self._transport.execute_step(job, step, prior_effects)
                except PublishTransportPartialError as exc:
                    partial_effects = self._effects_from_receipts(
                        plan.id,
                        step.index,
                        exc.receipts,
                    )
                    await self._repository.record_publish_effects(partial_effects)
                    await self._remember_references(job, tuple(exc.receipts))
                    log_event(
                        self._log,
                        logging.ERROR,
                        "publish.step.partial",
                        "Publish step produced only partial confirmed effects",
                        job_id=job.id,
                        plan_id=plan.id,
                        step_index=step.index,
                        receipt_count=len(exc.receipts),
                        error_code="publish_partial",
                        exception_type=type(exc).__name__,
                    )
                    raise
                active_receipts = tuple(receipts)
                effects = self._effects_from_receipts(plan.id, step.index, active_receipts)
                expected_effects = self._expected_effect_count(step)
                if len(active_receipts) < expected_effects:
                    # Preserve confirmed external side effects, but deliberately
                    # do not write the commit marker. Recovery will therefore
                    # classify this step as partial instead of replaying it.
                    await self._repository.record_publish_effects(effects)
                    raise IncompletePublishReceipts(
                        f"publish step {step.index} returned incomplete receipts: "
                        f"{len(active_receipts)}/{expected_effects}"
                    )
                marker = PublishEffect(
                    plan_id=plan.id,
                    step_index=step.index,
                    effect_type=self._COMMIT_EFFECT_TYPE,
                    detail={"receipt_count": len(receipts)},
                )
                await self._repository.record_publish_effects(effects + (marker,))
                await self._remember_references(job, active_receipts)
                await self._repository.update_publish_step_state(
                    plan.id,
                    step.index,
                    PublishStepState.SUCCEEDED,
                )
                log_event(
                    self._log,
                    logging.INFO,
                    "publish.step.completed",
                    job_id=job.id,
                    plan_id=plan.id,
                    step_index=step.index,
                    receipt_count=len(receipts),
                )
                await self._repository.set_job_progress(
                    JobProgress(
                        job_id=job.id,
                        phase="publishing",
                        current=step.index + 1,
                        total=len(plan.steps),
                        item_total=len(job.items),
                    )
                )
        except (JobCancelRequested, JobHoldRequested):
            raise
        except Exception as exc:
            if isinstance(exc, PublishTransportPartialError):
                failure_code = "publish_partial"
            elif isinstance(exc, (PublishTransportUncertainError, IncompletePublishReceipts)):
                failure_code = "publish_uncertain" if isinstance(
                    exc, PublishTransportUncertainError
                ) else "publish_partial"
            elif active_receipts:
                # Telegram has returned concrete message ids, but local
                # journaling/finalization failed afterwards. Replaying this
                # step could duplicate visible posts, even when the journal
                # transaction itself left no durable rows.
                failure_code = "publish_partial"
            else:
                failure_code = "publish_failed"
            log_event(
                self._log,
                logging.ERROR,
                "publish.job.failed",
                "Publish execution failed",
                job_id=job.id,
                plan_id=plan.id,
                step_index=active_step_index,
                error_code=failure_code,
                exception_type=type(exc).__name__,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                exc_info=True,
            )
            await self._repository.set_job_progress(
                JobProgress(
                    job_id=job.id,
                    phase="failed",
                    current=(active_step_index or 0),
                    total=len(plan.steps),
                    detail_code=failure_code,
                    item_total=len(job.items),
                )
            )
            try:
                if active_step_index is not None:
                    await self._repository.update_publish_step_state(
                        plan.id,
                        active_step_index,
                        PublishStepState.FAILED,
                        error_code=failure_code,
                        error_message=str(exc),
                    )
            finally:
                await self._repository.transition(
                    job.id,
                    JobState.FAILED,
                    event_type="publish_failed",
                    error_code=failure_code,
                    error_message=str(exc),
                )
            raise
        completed = await self._repository.transition(
            job.id,
            JobState.SUCCEEDED,
            event_type="publish_completed",
        )
        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="succeeded",
                current=len(plan.steps),
                total=len(plan.steps),
                item_total=len(job.items),
            )
        )
        log_event(
            self._log,
            logging.INFO,
            "publish.job.completed",
            job_id=job.id,
            plan_id=plan.id,
            step_count=len(plan.steps),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return completed

    async def _safe_checkpoint(self, job: Job, detail: str) -> None:
        if self._control is not None:
            await self._control.safe_checkpoint(job, detail=detail)

    async def _reconcile_inflight(self, job: Job, plan: PublishPlan) -> None:
        current = await self._repository.get_publish_plan(job.id)
        if current is None:
            raise RuntimeError("publish plan disappeared during recovery")
        effects = await self._repository.list_publish_effects(plan.id)
        by_step: dict[int, list[PublishEffect]] = {}
        for effect in effects:
            by_step.setdefault(effect.step_index, []).append(effect)

        for step in current.steps:
            if step.state != PublishStepState.RUNNING:
                continue
            confirmed = by_step.get(step.index, [])
            external = [
                effect for effect in confirmed if effect.effect_type != self._COMMIT_EFFECT_TYPE
            ]
            marker = next(
                (
                    effect
                    for effect in reversed(confirmed)
                    if effect.effect_type == self._COMMIT_EFFECT_TYPE
                ),
                None,
            )
            expected = self._expected_effect_count(step)
            marker_count = None
            if marker is not None:
                try:
                    marker_count = int(marker.detail.get("receipt_count"))
                except (TypeError, ValueError):
                    marker_count = None
            if marker_count == len(external) and len(external) >= expected:
                await self._repository.update_publish_step_state(
                    plan.id,
                    step.index,
                    PublishStepState.SUCCEEDED,
                )
                log_event(
                    self._log,
                    logging.INFO,
                    "publish.recovery.reconciled",
                    job_id=job.id,
                    plan_id=plan.id,
                    step_index=step.index,
                    confirmed_effects=len(external),
                )
                continue

            code = "publish_partial" if external else "publish_uncertain"
            message = (
                f"publish recovery stopped at step {step.index}: "
                f"confirmed_effects={len(external)} expected={expected} "
                f"commit_marker={marker_count!r}"
            )
            await self._repository.update_publish_step_state(
                plan.id,
                step.index,
                PublishStepState.FAILED,
                error_code=code,
                error_message=message,
            )
            await self._repository.transition(
                job.id,
                JobState.FAILED,
                event_type="publish_recovery_blocked",
                detail=f"plan={plan.id};step={step.index}",
                error_code=code,
                error_message=message,
            )
            await self._repository.set_job_progress(
                JobProgress(
                    job_id=job.id,
                    phase="failed",
                    current=step.index,
                    total=len(plan.steps),
                    detail_code=code,
                    item_total=len(job.items),
                )
            )
            log_event(
                self._log,
                logging.ERROR,
                "publish.recovery.blocked",
                "Publish recovery requires manual side-effect review",
                job_id=job.id,
                plan_id=plan.id,
                step_index=step.index,
                confirmed_effects=len(external),
                expected_effects=expected,
                error_code=code,
            )
            raise PublishRecoveryRequired(code, message, step_index=step.index)

    @staticmethod
    def _expected_effect_count(step) -> int:
        return max(1, len(step.item_indexes))

    @staticmethod
    def _effects_from_receipts(
        plan_id: str,
        step_index: int,
        receipts,
    ) -> tuple[PublishEffect, ...]:
        return tuple(
            PublishEffect(
                plan_id=plan_id,
                step_index=step_index,
                effect_type=receipt.effect_type,
                external_chat_id=receipt.external_chat_id,
                external_message_id=receipt.external_message_id,
                detail=receipt.detail,
            )
            for receipt in receipts
        )

    async def _remember_references(self, job: Job, receipts) -> None:
        by_index = {item.index: item for item in job.items}
        for receipt in receipts:
            reference = receipt.detail.get("reusable_ref")
            item_index = receipt.detail.get("item_index")
            if not reference or item_index is None:
                continue
            try:
                item = by_index[int(item_index)]
            except (KeyError, TypeError, ValueError):
                continue
            if not item.sha256:
                continue
            try:
                await self._repository.upsert_telegram_reference(
                    item.sha256,
                    job.destination,
                    item.kind,
                    str(reference),
                )
            except Exception:
                # Reference reuse is an optimization. A cache write must never
                # turn a confirmed Telegram publish into a failed job.
                log_event(
                    self._log,
                    logging.WARNING,
                    "publish.reference_cache.failed",
                    "Telegram reference cache write failed",
                    job_id=job.id,
                    item_index=item.index,
                    exc_info=True,
                )
