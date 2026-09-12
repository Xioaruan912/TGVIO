from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.job_control import (
    JobCancelRequested,
    JobControlService,
    JobHoldRequested,
    UnsafeRetryError,
)
from tgvio.application.orchestrator import JobOrchestrator
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.publish import PublishStepState
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class JobControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.repo = SQLiteJobRepository(self.path)
        await self.repo.open()
        self.control = JobControlService(self.repo)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_cancel_request_is_durable_and_checkpoint_cancels_active_job(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="fixture")],
        )
        await self.repo.create(job)
        job = await self.repo.transition(
            job.id,
            JobState.DOWNLOADING,
            event_type="download_started",
        )
        await self.control.request_cancel(job, reason="user requested")
        self.assertTrue(await self.repo.is_cancel_requested(job.id))

        await self.repo.close()
        self.repo = SQLiteJobRepository(self.path)
        await self.repo.open()
        self.control = JobControlService(self.repo)
        self.assertTrue(await self.repo.is_cancel_requested(job.id))

        with self.assertRaises(JobCancelRequested):
            await self.control.checkpoint(job, detail="safe download boundary")
        cancelled = await self.repo.get(job.id)
        assert cancelled is not None
        self.assertEqual(cancelled.state, JobState.CANCELLED)

    async def test_hold_is_durable_and_safe_checkpoint_stops_without_failing_job(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        await self.repo.create(job)
        job = await self.repo.transition(job.id, JobState.DOWNLOADING, event_type="download_started")
        held = await self.control.request_hold(job, reason="operator pause")
        self.assertTrue(held.hold_requested)
        self.assertEqual(held.hold_revision, 1)

        await self.repo.close()
        self.repo = SQLiteJobRepository(self.path)
        await self.repo.open()
        self.control = JobControlService(self.repo)
        durable = await self.repo.get_job_control(job.id)
        self.assertTrue(durable.hold_requested)

        with self.assertRaises(JobHoldRequested):
            await self.control.safe_checkpoint(job, detail="safe boundary")
        current = await self.repo.get(job.id)
        assert current is not None
        self.assertEqual(current.state, JobState.DOWNLOADING)
        self.assertIsNone(current.error_code)

        resumed = await self.control.resume(current)
        self.assertFalse(resumed.hold_requested)
        self.assertEqual(resumed.hold_revision, 2)
        await self.control.safe_checkpoint(current, detail="resumed boundary")

    async def test_cancel_supersedes_hold(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        await self.repo.create(job)
        job = await self.repo.transition(job.id, JobState.DOWNLOADING, event_type="download_started")
        await self.control.request_hold(job, reason="pause first")
        await self.control.request_cancel(job, reason="cancel instead")
        state = await self.repo.get_job_control(job.id)
        self.assertTrue(state.cancel_requested)
        self.assertFalse(state.hold_requested)
        with self.assertRaises(JobCancelRequested):
            await self.control.safe_checkpoint(job, detail="cancel boundary")

    async def test_global_queue_pause_is_durable_and_revisioned(self) -> None:
        paused = await self.control.pause_queue(reason="maintenance")
        self.assertTrue(paused.paused)
        self.assertEqual(paused.revision, 1)

        await self.repo.close()
        self.repo = SQLiteJobRepository(self.path)
        await self.repo.open()
        self.control = JobControlService(self.repo)
        durable = await self.repo.get_queue_control()
        self.assertTrue(durable.paused)
        self.assertEqual(durable.pause_reason, "maintenance")

        resumed = await self.control.resume_queue()
        self.assertFalse(resumed.paused)
        self.assertIsNone(resumed.pause_reason)
        self.assertEqual(resumed.revision, 2)

    async def test_planned_job_cancels_immediately_without_publish_side_effect(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.ANALYZED,
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="fixture")],
        )
        await self.repo.create(job)
        await JobOrchestrator(self.repo).mark_planned(job)
        planned = await self.repo.get(job.id)
        assert planned is not None
        cancelled = await self.control.request_cancel(planned, reason="no longer needed")
        self.assertEqual(cancelled.state, JobState.CANCELLED)

    async def test_prepublish_failure_retries_from_received_and_increments_counter(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        await self.repo.create(job)
        job = await self.repo.transition(job.id, JobState.DOWNLOADING, event_type="download_started")
        job = await self.repo.transition(
            job.id,
            JobState.FAILED,
            event_type="download_failed",
            error_code="download_failed",
            error_message="temporary",
        )
        await self.repo.request_cancel(job.id, reason="stale request")
        decision = await self.control.retry_failed(job)
        self.assertEqual(decision.target_state, JobState.RECEIVED)
        self.assertEqual(decision.retry_count, 1)
        self.assertFalse(await self.repo.is_cancel_requested(job.id))
        retried = await self.repo.get(job.id)
        assert retried is not None
        self.assertIsNone(retried.error_code)

    async def test_publish_failure_without_failed_step_effects_can_retry_from_planned(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.ANALYZED,
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="fixture")],
        )
        await self.repo.create(job)
        plan = await JobOrchestrator(self.repo).mark_planned(job)
        job = await self.repo.transition(job.id, JobState.PUBLISHING, event_type="publish_started")
        await self.repo.update_publish_step_state(
            plan.id,
            0,
            PublishStepState.FAILED,
            error_code="publish_step_failed",
            error_message="safe pre-send failure",
        )
        job = await self.repo.transition(
            job.id,
            JobState.FAILED,
            event_type="publish_failed",
            error_code="publish_failed",
            error_message="safe pre-send failure",
        )
        decision = await self.control.retry_failed(job)
        self.assertEqual(decision.target_state, JobState.PLANNED)
        loaded_plan = await self.repo.get_publish_plan(job.id)
        assert loaded_plan is not None
        self.assertEqual(loaded_plan.steps[0].state, PublishStepState.PENDING)

    async def test_partial_or_uncertain_publish_is_never_blindly_retried(self) -> None:
        for code in ("publish_partial", "publish_uncertain"):
            job = Job(
                owner_id=42,
                destination="@channel",
                items=[MediaItem(index=0, kind=MediaKind.PHOTO, source=code)],
            )
            await self.repo.create(job)
            job = await self.repo.transition(job.id, JobState.DOWNLOADING, event_type="download_started")
            job = await self.repo.transition(
                job.id,
                JobState.FAILED,
                event_type="publish_failed",
                error_code=code,
                error_message=code,
            )
            with self.assertRaises(UnsafeRetryError):
                await self.control.retry_failed(job)
