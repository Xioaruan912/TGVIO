from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.execution import PublishExecutionEngine, PublishRecoveryRequired
from tgvio.application.job_control import JobCancelRequested, JobControlService
from tgvio.application.job_runner import JobRunner
from tgvio.application.orchestrator import JobOrchestrator
from tgvio.application.ports import PublishTransportUncertainError
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.publish import PublishEffect, PublishReceipt, PublishStepState
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class RecordingTransport:
    def __init__(self, *, fail_on: int | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[int] = []
        self.prior_effect_counts: list[int] = []

    async def execute_step(self, job, step, prior_effects):
        self.calls.append(step.index)
        self.prior_effect_counts.append(len(prior_effects))
        if self.fail_on == step.index:
            raise RuntimeError(f"boom-{step.index}")
        return [
            PublishReceipt(
                effect_type="telegram_message",
                external_chat_id="-100123",
                external_message_id=str(9000 + step.index * 100 + offset),
                detail={"item": item_index},
            )
            for offset, item_index in enumerate(step.item_indexes)
        ]


class UncertainTransport:
    async def execute_step(self, job, step, prior_effects):
        raise PublishTransportUncertainError("Telegram response lost")


class CancelAfterFirstTransport(RecordingTransport):
    def __init__(self, repository) -> None:
        super().__init__()
        self.repository = repository

    async def execute_step(self, job, step, prior_effects):
        receipts = await super().execute_step(job, step, prior_effects)
        if step.index == 0:
            await self.repository.request_cancel(job.id, reason="stop after first step")
        return receipts


class PublishPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _planned_job(self) -> tuple[Job, object]:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.ANALYZED,
            items=[
                MediaItem(index=i, kind=MediaKind.PHOTO, source=f"photo-{i}")
                for i in range(12)
            ]
            + [
                MediaItem(
                    index=12,
                    kind=MediaKind.VIDEO,
                    source="video",
                    metadata={"telegram_streamable_candidate": True},
                )
            ],
        )
        await self.repo.create(job)
        orchestrator = JobOrchestrator(self.repo)
        plan = await orchestrator.mark_planned(job)
        return job, plan

    async def test_plan_steps_round_trip_and_effect_journal(self) -> None:
        job, plan = await self._planned_job()
        loaded = await self.repo.get_publish_plan(job.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.id, plan.id)
        self.assertEqual(len(loaded.steps), 3)
        self.assertTrue(all(step.state == PublishStepState.PENDING for step in loaded.steps))

        running = await self.repo.update_publish_step_state(
            plan.id,
            0,
            PublishStepState.RUNNING,
        )
        self.assertEqual(running.state, PublishStepState.RUNNING)
        await self.repo.record_publish_effect(
            PublishEffect(
                plan_id=plan.id,
                step_index=0,
                effect_type="telegram_message",
                external_chat_id="-1001",
                external_message_id="77",
            )
        )
        effects = await self.repo.list_publish_effects(plan.id)
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].external_message_id, "77")

    async def test_effect_batch_is_committed_atomically(self) -> None:
        job, plan = await self._planned_job()
        effects = tuple(
            PublishEffect(
                plan_id=plan.id,
                step_index=0,
                effect_type="telegram_channel_message",
                external_chat_id="-1001",
                external_message_id=str(700 + offset),
            )
            for offset in range(3)
        )
        recorded = await self.repo.record_publish_effects(effects)
        self.assertEqual([effect.external_message_id for effect in recorded], ["700", "701", "702"])
        loaded = await self.repo.list_publish_effects(plan.id)
        self.assertEqual([effect.external_message_id for effect in loaded], ["700", "701", "702"])

    async def test_execution_engine_completes_steps_and_job(self) -> None:
        job, plan = await self._planned_job()
        transport = RecordingTransport()
        completed = await PublishExecutionEngine(self.repo, transport).execute(job, plan)
        self.assertEqual(completed.state, JobState.SUCCEEDED)
        loaded_plan = await self.repo.get_publish_plan(job.id)
        assert loaded_plan is not None
        self.assertTrue(all(step.state == PublishStepState.SUCCEEDED for step in loaded_plan.steps))
        effects = await self.repo.list_publish_effects(plan.id)
        external_effects = [
            effect for effect in effects if effect.effect_type != "publish_step_receipts_committed"
        ]
        markers = [
            effect for effect in effects if effect.effect_type == "publish_step_receipts_committed"
        ]
        self.assertEqual(
            len(external_effects),
            sum(len(step.item_indexes) for step in plan.steps),
        )
        self.assertEqual(len(markers), len(plan.steps))
        self.assertEqual(transport.calls, list(range(len(plan.steps))))
        expected_prior = []
        running_total = 0
        for step in plan.steps:
            expected_prior.append(running_total)
            running_total += len(step.item_indexes)
        self.assertEqual(transport.prior_effect_counts, expected_prior)

    async def test_execution_failure_preserves_completed_side_effects(self) -> None:
        job, plan = await self._planned_job()
        transport = RecordingTransport(fail_on=1)
        with self.assertRaisesRegex(RuntimeError, "boom-1"):
            await PublishExecutionEngine(self.repo, transport).execute(job, plan)
        failed = await self.repo.get(job.id)
        assert failed is not None
        self.assertEqual(failed.state, JobState.FAILED)
        loaded_plan = await self.repo.get_publish_plan(job.id)
        assert loaded_plan is not None
        self.assertEqual(loaded_plan.steps[0].state, PublishStepState.SUCCEEDED)
        self.assertEqual(loaded_plan.steps[1].state, PublishStepState.FAILED)
        effects = await self.repo.list_publish_effects(plan.id)
        external_effects = [
            effect for effect in effects if effect.effect_type != "publish_step_receipts_committed"
        ]
        self.assertEqual(len(external_effects), len(plan.steps[0].item_indexes))
        self.assertTrue(all(effect.step_index == 0 for effect in effects))

    async def test_receipt_journal_failure_is_never_classified_as_safe_to_retry(self) -> None:
        job, plan = await self._planned_job()
        transport = RecordingTransport()

        async def fail_receipt_journal(_effects):
            raise RuntimeError("fixture receipt journal unavailable")

        self.repo.record_publish_effects = fail_receipt_journal
        with self.assertRaisesRegex(RuntimeError, "receipt journal unavailable"):
            await PublishExecutionEngine(self.repo, transport).execute(job, plan)

        failed = await self.repo.get(job.id)
        assert failed is not None
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.error_code, "publish_partial")
        loaded_plan = await self.repo.get_publish_plan(job.id)
        assert loaded_plan is not None
        self.assertEqual(loaded_plan.steps[0].error_code, "publish_partial")

    async def test_uncertain_visible_send_is_classified_for_manual_review(self) -> None:
        job, plan = await self._planned_job()
        with self.assertRaises(PublishTransportUncertainError):
            await PublishExecutionEngine(self.repo, UncertainTransport()).execute(job, plan)
        failed = await self.repo.get(job.id)
        assert failed is not None
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.error_code, "publish_uncertain")
        loaded_plan = await self.repo.get_publish_plan(job.id)
        assert loaded_plan is not None
        self.assertEqual(loaded_plan.steps[0].error_code, "publish_uncertain")

    async def test_cancel_after_completed_step_preserves_effects_and_stops_next_step(self) -> None:
        job, plan = await self._planned_job()
        control = JobControlService(self.repo)
        transport = CancelAfterFirstTransport(self.repo)
        with self.assertRaises(JobCancelRequested):
            await PublishExecutionEngine(self.repo, transport, control).execute(job, plan)
        cancelled = await self.repo.get(job.id)
        assert cancelled is not None
        self.assertEqual(cancelled.state, JobState.CANCELLED)
        self.assertEqual(transport.calls, [0])
        loaded_plan = await self.repo.get_publish_plan(job.id)
        assert loaded_plan is not None
        self.assertEqual(loaded_plan.steps[0].state, PublishStepState.SUCCEEDED)
        self.assertEqual(loaded_plan.steps[1].state, PublishStepState.PENDING)
        effects = await self.repo.list_publish_effects(plan.id)
        self.assertTrue(any(effect.step_index == 0 for effect in effects))
        self.assertFalse(any(effect.step_index == 1 for effect in effects))

    async def test_recent_jobs_and_state_counts_support_ui(self) -> None:
        first = Job(owner_id=42, destination="@channel", items=[])
        second = Job(owner_id=42, destination="@channel", items=[])
        foreign = Job(owner_id=99, destination="@channel", items=[])
        await self.repo.create(first)
        await self.repo.create(second)
        await self.repo.create(foreign)
        await self.repo.transition(second.id, JobState.DOWNLOADED, event_type="download_skipped")
        recent = await self.repo.list_recent(owner_id=42, limit=5)
        self.assertEqual({job.id for job in recent}, {first.id, second.id})
        counts = await self.repo.count_by_state(owner_id=42)
        self.assertEqual(counts[JobState.RECEIVED], 1)
        self.assertEqual(counts[JobState.DOWNLOADED], 1)

    async def test_job_runner_stops_at_planned_when_execution_is_disabled(self) -> None:
        job, _plan = await self._planned_job()

        class NeverCalledIngestion:
            async def process(self, _job):
                raise AssertionError("planned job must not re-enter ingestion")

        completed = await JobRunner(self.repo, NeverCalledIngestion(), None).process(job)
        self.assertEqual(completed.state, JobState.PLANNED)

    async def test_job_runner_executes_only_when_execution_is_wired(self) -> None:
        job, plan = await self._planned_job()

        class NeverCalledIngestion:
            async def process(self, _job):
                raise AssertionError("planned job must not re-enter ingestion")

        class RecordingExecution:
            def __init__(self) -> None:
                self.plan_id = None

            async def execute(self, current_job, current_plan):
                self.plan_id = current_plan.id
                current_job.state = JobState.SUCCEEDED
                return current_job

        execution = RecordingExecution()
        completed = await JobRunner(self.repo, NeverCalledIngestion(), execution).process(job)
        self.assertEqual(completed.state, JobState.SUCCEEDED)
        self.assertEqual(execution.plan_id, plan.id)

    async def test_successful_receipt_refreshes_durable_telegram_reference_cache(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.ANALYZED,
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.PHOTO,
                    source="photo",
                    sha256="cache-sha",
                )
            ],
        )
        await self.repo.create(job)
        plan = await JobOrchestrator(self.repo).mark_planned(job)

        class CacheTransport:
            async def execute_step(self, _job, step, _prior_effects):
                return [
                    PublishReceipt(
                        effect_type="telegram_channel_message",
                        external_chat_id="-100321",
                        external_message_id="654",
                        detail={
                            "item_index": step.item_indexes[0],
                            "reusable_ref": "telegram:-100321:654",
                        },
                    )
                ]

        completed = await PublishExecutionEngine(self.repo, CacheTransport()).execute(job, plan)
        self.assertEqual(completed.state, JobState.SUCCEEDED)
        self.assertEqual(
            await self.repo.get_telegram_reference(
                "cache-sha", "@channel", MediaKind.PHOTO
            ),
            "telegram:-100321:654",
        )

    async def test_publishing_recovery_skips_succeeded_steps_and_continues_pending(self) -> None:
        job, plan = await self._planned_job()
        job = await self.repo.transition(job.id, JobState.PUBLISHING, event_type="publish_started")
        await self.repo.update_publish_step_state(plan.id, 0, PublishStepState.SUCCEEDED)
        transport = RecordingTransport()
        completed = await PublishExecutionEngine(self.repo, transport).execute(job, plan)
        self.assertEqual(completed.state, JobState.SUCCEEDED)
        self.assertEqual(transport.calls, [1, 2])

    async def test_running_step_with_complete_effects_is_reconciled_without_resend(self) -> None:
        job, plan = await self._planned_job()
        job = await self.repo.transition(job.id, JobState.PUBLISHING, event_type="publish_started")
        await self.repo.update_publish_step_state(plan.id, 0, PublishStepState.RUNNING)
        for index in plan.steps[0].item_indexes:
            await self.repo.record_publish_effect(
                PublishEffect(
                    plan_id=plan.id,
                    step_index=0,
                    effect_type="telegram_channel_message",
                    external_chat_id="-1001",
                    external_message_id=str(1000 + index),
                )
            )
        await self.repo.record_publish_effect(
            PublishEffect(
                plan_id=plan.id,
                step_index=0,
                effect_type="publish_step_receipts_committed",
                detail={"receipt_count": len(plan.steps[0].item_indexes)},
            )
        )
        transport = RecordingTransport()
        completed = await PublishExecutionEngine(self.repo, transport).execute(job, plan)
        self.assertEqual(completed.state, JobState.SUCCEEDED)
        self.assertEqual(transport.calls, [1, 2])

    async def test_running_step_with_effects_but_no_commit_marker_is_partial(self) -> None:
        job, plan = await self._planned_job()
        job = await self.repo.transition(job.id, JobState.PUBLISHING, event_type="publish_started")
        await self.repo.update_publish_step_state(plan.id, 0, PublishStepState.RUNNING)
        for index in plan.steps[0].item_indexes:
            await self.repo.record_publish_effect(
                PublishEffect(
                    plan_id=plan.id,
                    step_index=0,
                    effect_type="telegram_channel_message",
                    external_chat_id="-1001",
                    external_message_id=str(2000 + index),
                )
            )
        transport = RecordingTransport()
        with self.assertRaises(PublishRecoveryRequired) as ctx:
            await PublishExecutionEngine(self.repo, transport).execute(job, plan)
        self.assertEqual(ctx.exception.code, "publish_partial")
        self.assertEqual(transport.calls, [])

    async def test_running_step_without_effects_blocks_recovery_instead_of_resending(self) -> None:
        job, plan = await self._planned_job()
        job = await self.repo.transition(job.id, JobState.PUBLISHING, event_type="publish_started")
        await self.repo.update_publish_step_state(plan.id, 0, PublishStepState.RUNNING)
        transport = RecordingTransport()
        with self.assertRaises(PublishRecoveryRequired) as ctx:
            await PublishExecutionEngine(self.repo, transport).execute(job, plan)
        self.assertEqual(ctx.exception.code, "publish_uncertain")
        self.assertEqual(transport.calls, [])
        failed = await self.repo.get(job.id)
        assert failed is not None
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.error_code, "publish_uncertain")

    async def test_running_step_with_partial_effects_blocks_recovery(self) -> None:
        job, plan = await self._planned_job()
        job = await self.repo.transition(job.id, JobState.PUBLISHING, event_type="publish_started")
        await self.repo.update_publish_step_state(plan.id, 0, PublishStepState.RUNNING)
        await self.repo.record_publish_effect(
            PublishEffect(
                plan_id=plan.id,
                step_index=0,
                effect_type="telegram_channel_message",
                external_chat_id="-1001",
                external_message_id="1000",
            )
        )
        transport = RecordingTransport()
        with self.assertRaises(PublishRecoveryRequired) as ctx:
            await PublishExecutionEngine(self.repo, transport).execute(job, plan)
        self.assertEqual(ctx.exception.code, "publish_partial")
        self.assertEqual(transport.calls, [])
        failed = await self.repo.get(job.id)
        assert failed is not None
        self.assertEqual(failed.error_code, "publish_partial")
