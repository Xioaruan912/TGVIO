from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.archive_planner import ArchivePlanner
from tgvio.application.cache_cleanup import CacheCleanupService
from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.domain.archive import ArchivePackageState, ArchivePolicy, ArchiveProfileSnapshot
from tgvio.domain.job import JobState, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class CacheCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.downloads = self.root / "downloads"
        self.repo = SQLiteJobRepository(self.root / "state.sqlite3")
        await self.repo.open()
        self._source_message_id = 0

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _job_with_cache(self, state: JobState, *, payload: bytes = b"payload"):
        self._source_message_id += 1
        message_id = self._source_message_id
        job = await IntakeService(self.repo).accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.DOCUMENT,
                    source=f"telegram:42:{message_id}",
                    source_chat_id=42,
                    source_message_id=message_id,
                )
            ],
        )
        job_dir = self.downloads / f"job-{job.id}"
        job_dir.mkdir(parents=True, exist_ok=True)
        local = job_dir / "000-file.bin"
        local.write_bytes(payload)
        job.items[0] = type(job.items[0])(
            **{
                field: getattr(job.items[0], field)
                for field in job.items[0].__dataclass_fields__
                if field not in {"local_path", "size_bytes", "sha256"}
            },
            local_path=str(local),
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        job.state = state
        await self.repo.save(job)
        return job, local

    async def test_force_cleanup_removes_succeeded_cache_and_clears_local_path(self) -> None:
        job, local = await self._job_with_cache(JobState.SUCCEEDED)
        service = CacheCleanupService(self.repo, self.downloads, retention_hours=24)
        result = await service.cleanup(force=True)
        self.assertEqual(result.removed_jobs, 1)
        self.assertEqual(result.removed_bytes, len(b"payload"))
        self.assertFalse(local.exists())
        loaded = await self.repo.get(job.id)
        assert loaded is not None
        self.assertIsNone(loaded.items[0].local_path)
        self.assertTrue(loaded.items[0].metadata["cache_cleaned"])

    async def test_force_cleanup_removes_cancelled_but_never_planned_or_failed(self) -> None:
        cancelled, cancelled_path = await self._job_with_cache(JobState.CANCELLED, payload=b"cancel")
        planned, planned_path = await self._job_with_cache(JobState.PLANNED, payload=b"plan")
        failed, failed_path = await self._job_with_cache(JobState.FAILED, payload=b"failed")
        service = CacheCleanupService(self.repo, self.downloads, retention_hours=24)
        result = await service.cleanup(force=True)
        self.assertEqual(result.removed_jobs, 1)
        self.assertFalse(cancelled_path.exists())
        self.assertTrue(planned_path.exists())
        self.assertTrue(failed_path.exists())
        self.assertEqual((await self.repo.get(cancelled.id)).state, JobState.CANCELLED)
        self.assertEqual((await self.repo.get(planned.id)).state, JobState.PLANNED)
        self.assertEqual((await self.repo.get(failed.id)).state, JobState.FAILED)

    async def test_uncommitted_archive_package_blocks_cleanup(self) -> None:
        job, local = await self._job_with_cache(JobState.SUCCEEDED)
        package = await self.repo.save_archive_plan(ArchivePlanner().plan(job))
        self.assertEqual(package.state.value, "planned")
        service = CacheCleanupService(self.repo, self.downloads, retention_hours=24)
        result = await service.cleanup(force=True)
        self.assertEqual(result.removed_jobs, 0)
        self.assertEqual(result.blocked_by_archive, 1)
        self.assertTrue(local.exists())

    async def test_best_effort_archive_still_protects_cache_while_active(self) -> None:
        job, local = await self._job_with_cache(JobState.SUCCEEDED)
        package = await self.repo.save_archive_plan(
            ArchivePlanner(
                profile=ArchiveProfileSnapshot(policy=ArchivePolicy.BEST_EFFORT)
            ).plan(job)
        )
        self.assertEqual(package.state, ArchivePackageState.PLANNED)
        result = await CacheCleanupService(
            self.repo,
            self.downloads,
            retention_hours=24,
        ).cleanup(force=True)
        self.assertEqual(result.removed_jobs, 0)
        self.assertEqual(result.blocked_by_archive, 1)
        self.assertTrue(local.exists())

    async def test_failed_required_archive_keeps_cache_but_best_effort_can_release_after_recovery_decision(self) -> None:
        required_job, required_path = await self._job_with_cache(
            JobState.SUCCEEDED,
            payload=b"required",
        )
        required = await self.repo.save_archive_plan(ArchivePlanner().plan(required_job))
        await self.repo.update_archive_package_state(
            required.id,
            ArchivePackageState.FAILED,
            event_type="archive_failed",
            error_code="fixture",
        )

        best_job, best_path = await self._job_with_cache(
            JobState.SUCCEEDED,
            payload=b"best-effort",
        )
        best_job.policy["auto_recovery"] = {
            "version": 1,
            "enabled": True,
            "max_attempts": 3,
            "base_delay_seconds": 15,
            "max_delay_seconds": 300,
        }
        await self.repo.save(best_job)
        best = await self.repo.save_archive_plan(
            ArchivePlanner(
                profile=ArchiveProfileSnapshot(policy=ArchivePolicy.BEST_EFFORT)
            ).plan(best_job)
        )
        await self.repo.update_archive_package_state(
            best.id,
            ArchivePackageState.FAILED,
            event_type="archive_failed",
            error_code="fixture",
        )

        service = CacheCleanupService(self.repo, self.downloads, retention_hours=24)
        waiting = await service.cleanup(force=True)
        self.assertEqual(waiting.removed_jobs, 0)
        self.assertEqual(waiting.blocked_by_archive, 2)
        self.assertTrue(required_path.exists())
        self.assertTrue(best_path.exists())

        best_job = await self.repo.get(best_job.id)
        assert best_job is not None
        best_job.policy["auto_recovery_archive"] = {
            "status": "exhausted",
            "failure_id": "fixture",
        }
        await self.repo.save(best_job)
        completed = await service.cleanup(force=True)
        self.assertEqual(completed.removed_jobs, 1)
        self.assertEqual(completed.blocked_by_archive, 1)
        self.assertTrue(required_path.exists())
        self.assertFalse(best_path.exists())

    async def test_non_force_cleanup_waits_for_retention_then_cleans(self) -> None:
        job, local = await self._job_with_cache(JobState.SUCCEEDED)
        service = CacheCleanupService(self.repo, self.downloads, retention_hours=24)
        first = await service.cleanup(force=False)
        self.assertEqual(first.removed_jobs, 0)
        self.assertTrue(local.exists())
        conn = self.repo._require()
        await conn.execute(
            "UPDATE jobs SET updated_at='2000-01-01 00:00:00' WHERE id=?",
            (job.id,),
        )
        await conn.commit()
        second = await service.cleanup(force=False)
        self.assertEqual(second.removed_jobs, 1)
        self.assertFalse(local.exists())

    async def test_stats_reports_usage_eligibility_and_archive_block(self) -> None:
        eligible, eligible_path = await self._job_with_cache(JobState.SUCCEEDED, payload=b"abc")
        blocked, blocked_path = await self._job_with_cache(JobState.CANCELLED, payload=b"12345")
        conn = self.repo._require()
        await conn.execute(
            "UPDATE jobs SET updated_at='2000-01-01 00:00:00' WHERE id IN (?,?)",
            (eligible.id, blocked.id),
        )
        await conn.commit()
        await self.repo.save_archive_plan(ArchivePlanner().plan(blocked))
        stats = await CacheCleanupService(
            self.repo,
            self.downloads,
            retention_hours=24,
        ).stats()
        self.assertEqual(stats.bytes_used, eligible_path.stat().st_size + blocked_path.stat().st_size)
        self.assertEqual(stats.managed_dirs, 2)
        self.assertEqual(stats.eligible_jobs, 1)
        self.assertEqual(stats.blocked_by_archive, 1)

    async def test_exact_cleanup_candidates_do_not_expand_during_execution(self) -> None:
        first, first_path = await self._job_with_cache(JobState.SUCCEEDED, payload=b"one")
        service = CacheCleanupService(self.repo, self.downloads, retention_hours=24)
        candidates = await service.cleanup_candidates(force=True)
        self.assertEqual(candidates, (first.id,))

        second, second_path = await self._job_with_cache(JobState.SUCCEEDED, payload=b"two")
        result = await service.cleanup(force=True, job_ids=candidates)

        self.assertEqual(result.removed_jobs, 1)
        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertEqual(await service.cleanup_candidates(force=True), (second.id,))

    async def test_already_cleaned_job_is_not_reported_again(self) -> None:
        job, _local = await self._job_with_cache(JobState.SUCCEEDED)
        connection = self.repo._require()
        await connection.execute(
            "UPDATE jobs SET updated_at='2000-01-01 00:00:00' WHERE id=?",
            (job.id,),
        )
        await connection.commit()
        service = CacheCleanupService(self.repo, self.downloads, retention_hours=24)

        first = await service.cleanup(force=False)
        stats = await service.stats()
        second = await service.cleanup(force=False)

        self.assertEqual(first.removed_jobs, 1)
        self.assertEqual(stats.eligible_jobs, 0)
        self.assertEqual(second.removed_jobs, 0)
