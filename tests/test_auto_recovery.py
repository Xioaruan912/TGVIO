from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tgvio.application.archive_planner import ArchivePlanner
from tgvio.application.archive_runtime import ArchiveService
from tgvio.application.auto_recovery import (
    ARCHIVE_RECOVERY_STATE_KEY,
    AUTO_RECOVERY_POLICY_KEY,
    JOB_RECOVERY_STATE_KEY,
    AutoRecoveryPolicy,
    AutoRecoveryService,
    archive_failure_waits_for_recovery,
    job_failure_waits_for_recovery,
)
from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.job_control import JobControlService
from tgvio.application.orchestrator import JobOrchestrator
from tgvio.domain.archive import ArchivePackageState
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.publish import PublishStepState
from tgvio.infrastructure.sqlite import SQLiteJobRepository
from tests.test_archive_executor import MemoryArchiveTransport


class _Clock:
    def __init__(self, value: int = 1_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return float(self.value)

    def advance(self, seconds: int) -> None:
        self.value += int(seconds)


class _RecordingCache:
    def __init__(self) -> None:
        self.calls = 0

    async def cleanup(self, *, force: bool = False):
        self.calls += 1
        return SimpleNamespace(removed_jobs=2, removed_bytes=4096, blocked_by_archive=0)


class AutoRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = self.root / "state.sqlite3"
        self.repo = SQLiteJobRepository(self.database)
        await self.repo.open()
        self.clock = _Clock()
        self.policy = AutoRecoveryPolicy(
            max_attempts=2,
            base_delay_seconds=1,
            max_delay_seconds=4,
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    def _policy(self) -> dict[str, object]:
        return {AUTO_RECOVERY_POLICY_KEY: self.policy.frozen()}

    async def _failed_download(
        self,
        *,
        code: str = "download_failed",
        stamped: bool = True,
    ) -> Job:
        job = Job(
            owner_id=42,
            destination="@channel",
            policy=self._policy() if stamped else {},
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        await self.repo.create(job)
        job = await self.repo.transition(
            job.id,
            JobState.DOWNLOADING,
            event_type="download_started",
        )
        return await self.repo.transition(
            job.id,
            JobState.FAILED,
            event_type="download_failed",
            error_code=code,
            error_message="safe fixture failure",
        )

    def _service(self, **kwargs) -> AutoRecoveryService:
        return AutoRecoveryService(
            self.repo,
            JobControlService(self.repo),
            clock=self.clock,
            **kwargs,
        )

    async def test_intake_freezes_policy_only_when_composition_supplies_it(self) -> None:
        stamped = await IntakeService(
            self.repo,
            default_policy=self._policy(),
        ).accept(
            owner_id=42,
            destination="@channel",
            media=[IncomingMedia(kind=MediaKind.VIDEO, source="fixture:stamped")],
            policy={"display_expected": True},
        )
        legacy = await IntakeService(self.repo).accept(
            owner_id=42,
            destination="@channel",
            media=[IncomingMedia(kind=MediaKind.VIDEO, source="fixture:legacy")],
        )

        self.assertEqual(stamped.policy[AUTO_RECOVERY_POLICY_KEY], self.policy.frozen())
        self.assertTrue(stamped.policy["display_expected"])
        self.assertNotIn(AUTO_RECOVERY_POLICY_KEY, legacy.policy)

    async def test_download_retry_survives_restart_and_exhausts_with_backoff(self) -> None:
        failed = await self._failed_download()
        first_service = self._service()

        scheduled = await first_service.run_once()
        self.assertEqual(scheduled.scheduled_jobs, 1)
        waiting = await self.repo.get(failed.id)
        assert waiting is not None
        self.assertTrue(job_failure_waits_for_recovery(waiting))
        self.assertEqual(waiting.policy[JOB_RECOVERY_STATE_KEY]["next_retry_epoch"], 1001)

        await self.repo.close()
        self.repo = SQLiteJobRepository(self.database)
        await self.repo.open()
        self.clock.advance(1)
        retried = await self._service().run_once()
        self.assertEqual(len(retried.retried_jobs), 1)
        self.assertEqual(retried.retried_jobs[0].state, JobState.RECEIVED)
        self.assertEqual(await self.repo.get_retry_count(failed.id), 1)

        current = await self.repo.transition(
            failed.id,
            JobState.DOWNLOADING,
            event_type="download_started",
        )
        current = await self.repo.transition(
            current.id,
            JobState.FAILED,
            event_type="download_failed",
            error_code="download_failed",
            error_message="fixture failed again",
        )
        second_wait = await self._service().run_once()
        self.assertEqual(second_wait.scheduled_jobs, 1)
        current = await self.repo.get(current.id)
        assert current is not None
        self.assertEqual(current.policy[JOB_RECOVERY_STATE_KEY]["next_retry_epoch"], 1003)

        self.clock.advance(2)
        second_retry = await self._service().run_once()
        self.assertEqual(len(second_retry.retried_jobs), 1)
        current = await self.repo.transition(
            failed.id,
            JobState.DOWNLOADING,
            event_type="download_started",
        )
        await self.repo.transition(
            current.id,
            JobState.FAILED,
            event_type="download_failed",
            error_code="download_failed",
            error_message="fixture permanently failed",
        )

        exhausted = await self._service().run_once()
        self.assertEqual(exhausted.exhausted_jobs, 1)
        current = await self.repo.get(failed.id)
        assert current is not None
        self.assertEqual(current.state, JobState.FAILED)
        self.assertEqual(current.policy[JOB_RECOVERY_STATE_KEY]["status"], "exhausted")
        self.assertFalse(job_failure_waits_for_recovery(current))
        self.assertEqual((await self._service().run_once()).scanned_jobs, 0)

    async def test_disk_low_runs_safe_completed_cache_cleanup_before_retry(self) -> None:
        failed = await self._failed_download(code="disk_low")
        cache = _RecordingCache()
        service = self._service(cache_operator=cache)
        await service.run_once()
        self.clock.advance(1)
        result = await service.run_once()

        self.assertEqual(cache.calls, 1)
        self.assertEqual([job.id for job in result.retried_jobs], [failed.id])

    async def test_telegram_file_timeout_backs_off_in_minutes_and_stops_after_two(self) -> None:
        failed = await self._failed_download(code="telegram_file_timeout")
        service = self._service()

        scheduled = await service.run_once()
        self.assertEqual(scheduled.scheduled_jobs, 1)
        waiting = await self.repo.get(failed.id)
        assert waiting is not None
        # 900s base delay instead of the 15s used for ordinary failures.
        self.assertEqual(
            waiting.policy[JOB_RECOVERY_STATE_KEY]["next_retry_epoch"], 1900
        )
        self.assertEqual(
            waiting.policy[JOB_RECOVERY_STATE_KEY]["max_attempts"], 2
        )

    async def test_old_unstamped_job_is_never_automatically_retried(self) -> None:
        failed = await self._failed_download(stamped=False)
        first = await self._service().run_once()
        self.clock.advance(100)
        second = await self._service().run_once()

        self.assertEqual(first.scheduled_jobs, 0)
        self.assertEqual(second.retried_jobs, ())
        current = await self.repo.get(failed.id)
        assert current is not None
        self.assertEqual(current.state, JobState.FAILED)
        self.assertNotIn(JOB_RECOVERY_STATE_KEY, current.policy)

    async def test_unknown_failure_is_abandoned_without_waiting_for_user(self) -> None:
        failed = await self._failed_download(code="permanent_fixture_error")
        result = await self._service().run_once()

        self.assertEqual(result.exhausted_jobs, 1)
        current = await self.repo.get(failed.id)
        assert current is not None
        self.assertEqual(current.policy[JOB_RECOVERY_STATE_KEY]["status"], "abandoned")
        self.assertEqual(await self.repo.get_retry_count(failed.id), 0)
        self.assertEqual((await self._service().run_once()).scanned_jobs, 0)

    async def test_safe_publish_failure_retries_only_the_failed_plan_step(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.ANALYZED,
            policy=self._policy(),
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="fixture")],
        )
        await self.repo.create(job)
        plan = await JobOrchestrator(self.repo).mark_planned(job)
        job = await self.repo.transition(job.id, JobState.PUBLISHING, event_type="publish_started")
        await self.repo.update_publish_step_state(
            plan.id,
            0,
            PublishStepState.FAILED,
            error_code="publish_failed",
            error_message="failed before send",
        )
        await self.repo.transition(
            job.id,
            JobState.FAILED,
            event_type="publish_failed",
            error_code="publish_failed",
            error_message="failed before send",
        )

        await self._service().run_once()
        self.clock.advance(1)
        result = await self._service().run_once()

        self.assertEqual(len(result.retried_jobs), 1)
        self.assertEqual(result.retried_jobs[0].state, JobState.PLANNED)
        loaded = await self.repo.get_publish_plan(job.id)
        assert loaded is not None
        self.assertEqual(loaded.steps[0].state, PublishStepState.PENDING)

    async def test_uncertain_publish_is_quarantined_without_blocking_later_job(self) -> None:
        first = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            policy=self._policy(),
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="fixture:first")],
        )
        await self.repo.create(first)
        first = await self.repo.transition(
            first.id,
            JobState.PUBLISHING,
            event_type="publish_started",
        )
        await self.repo.transition(
            first.id,
            JobState.FAILED,
            event_type="publish_failed",
            error_code="publish_uncertain",
            error_message="response missing",
        )
        later = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            policy=self._policy(),
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="fixture:later")],
        )
        await self.repo.create(later)

        blocked = await self.repo.get_next_publish_gate()
        assert blocked is not None
        self.assertEqual(blocked.job_id, first.id)
        self.assertTrue(blocked.blocks_for_manual_review)

        result = await self._service().run_once()
        self.assertEqual(result.quarantined_jobs, 1)
        quarantined = await self.repo.get(first.id)
        assert quarantined is not None
        self.assertEqual(
            quarantined.policy[JOB_RECOVERY_STATE_KEY]["status"],
            "quarantined",
        )
        gate = await self.repo.get_next_publish_gate()
        assert gate is not None
        self.assertEqual(gate.job_id, later.id)
        self.assertEqual(await self.repo.get_retry_count(first.id), 0)

    async def test_archive_failure_retries_same_package_and_reuses_remote_object(self) -> None:
        payload = b"archive-auto-recovery"
        local = self.root / "archive.bin"
        local.write_bytes(payload)
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            policy=self._policy(),
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.DOCUMENT,
                    source="fixture",
                    local_path=str(local),
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            ],
        )
        await self.repo.create(job)
        transport = MemoryArchiveTransport()
        archive = ArchiveService(self.repo, ArchivePlanner(), transport)
        package = await archive.enqueue_job(job)
        assert package is not None
        transport.fail_complete_once = True
        await archive.run_pending_once()
        failed = await self.repo.get_archive_package(package.id)
        assert failed is not None
        self.assertEqual(failed.state, ArchivePackageState.FAILED)

        recovery = self._service(archive_operator=archive)
        waiting = await recovery.run_once()
        self.assertEqual(waiting.scheduled_archives, 1)
        current_job = await self.repo.get(job.id)
        assert current_job is not None
        self.assertTrue(archive_failure_waits_for_recovery(current_job, failed))

        self.clock.advance(1)
        retried = await recovery.run_once()
        self.assertEqual(retried.retried_archives, 1)
        reset = await self.repo.get_archive_package(package.id)
        assert reset is not None
        self.assertEqual(reset.state, ArchivePackageState.STAGING)
        events = await self.repo.list_archive_events(package.id)
        self.assertIn("archive_auto_retry_requested", {event.event_type for event in events})

        remote_media = f"{package.remote_path}/{package.objects[0].remote_relpath}"
        self.assertEqual(transport.put_counts[remote_media], 1)
        await archive.run_pending_once()
        completed = await self.repo.get_archive_package(package.id)
        assert completed is not None
        self.assertEqual(completed.state, ArchivePackageState.COMMITTED)
        self.assertEqual(transport.put_counts[remote_media], 1)
        current_job = await self.repo.get(job.id)
        assert current_job is not None
        self.assertEqual(
            current_job.policy[ARCHIVE_RECOVERY_STATE_KEY]["attempt_count"],
            1,
        )

    async def test_unstamped_archive_failure_stays_manual(self) -> None:
        payload = b"legacy-archive"
        local = self.root / "legacy.bin"
        local.write_bytes(payload)
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.DOCUMENT,
                    source="fixture",
                    local_path=str(local),
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            ],
        )
        await self.repo.create(job)
        transport = MemoryArchiveTransport()
        archive = ArchiveService(self.repo, ArchivePlanner(), transport)
        package = await archive.enqueue_job(job)
        assert package is not None
        transport.fail_once_suffix = package.objects[0].remote_relpath
        await archive.run_pending_once()

        result = await self._service(archive_operator=archive).run_once()
        self.assertEqual(result.scheduled_archives, 0)
        failed = await self.repo.get_archive_package(package.id)
        assert failed is not None
        self.assertEqual(failed.state, ArchivePackageState.FAILED)

    async def test_archive_missing_canonical_cache_is_abandoned_without_looping(self) -> None:
        payload = b"missing-after-failure"
        local = self.root / "missing.bin"
        local.write_bytes(payload)
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            policy=self._policy(),
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.DOCUMENT,
                    source="fixture",
                    local_path=str(local),
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            ],
        )
        await self.repo.create(job)
        transport = MemoryArchiveTransport()
        transport.fail_complete_once = True
        archive = ArchiveService(self.repo, ArchivePlanner(), transport)
        package = await archive.enqueue_job(job)
        assert package is not None
        await archive.run_pending_once()
        local.unlink()

        recovery = self._service(archive_operator=archive)
        await recovery.run_once()
        self.clock.advance(1)
        result = await recovery.run_once()

        self.assertEqual(result.exhausted_archives, 1)
        current = await self.repo.get(job.id)
        assert current is not None
        state = current.policy[ARCHIVE_RECOVERY_STATE_KEY]
        self.assertEqual(state["status"], "abandoned")
        self.assertEqual(state["reason"], "canonical_cache_unavailable")
        failed = await self.repo.get_archive_package(package.id)
        assert failed is not None
        self.assertEqual(failed.state, ArchivePackageState.FAILED)
        self.assertEqual((await recovery.run_once()).scanned_archives, 0)
