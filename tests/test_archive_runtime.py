from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.archive_planner import ArchivePlanner
from tgvio.application.archive_runtime import ArchiveService
from tgvio.application.job_runner import JobRunner
from tgvio.domain.archive import ArchivePackageState
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository
from tests.test_archive_executor import MemoryArchiveTransport


class ArchiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = SQLiteJobRepository(self.root / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _job(self, suffix: str = "a") -> Job:
        local = self.root / f"{suffix}.bin"
        payload = f"archive-runtime-{suffix}".encode()
        local.write_bytes(payload)
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            created_at="2026-09-04 01:19:23",
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.DOCUMENT,
                    source="fixture",
                    local_path=str(local),
                    name=f"{suffix}.bin",
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            ],
        )
        await self.repo.create(job)
        return job

    async def test_enqueue_is_idempotent_and_one_job_has_one_package(self) -> None:
        job = await self._job()
        service = ArchiveService(
            self.repo,
            ArchivePlanner(remote_root="TGVIO"),
            MemoryArchiveTransport(),
        )
        first = await service.enqueue_job(job)
        second = await service.enqueue_job(job)
        assert first is not None and second is not None
        self.assertEqual(first.id, second.id)
        self.assertTrue(first.remote_path.startswith("TGVIO/archive/"))
        self.assertTrue(first.staging_path.startswith("TGVIO/.staging/"))
        self.assertEqual(len(await self.repo.list_recent_archive_packages(owner_id=42)), 1)

    async def test_run_pending_commits_archive_without_mutating_telegram_job(self) -> None:
        job = await self._job("commit")
        service = ArchiveService(self.repo, ArchivePlanner(), MemoryArchiveTransport())
        package = await service.enqueue_job(job)
        assert package is not None
        processed = await service.run_pending_once()
        self.assertEqual(processed, 1)
        stored = await self.repo.get_archive_package(package.id)
        assert stored is not None
        self.assertEqual(stored.state, ArchivePackageState.COMMITTED)
        durable_job = await self.repo.get(job.id)
        assert durable_job is not None
        self.assertEqual(durable_job.state, JobState.PLANNED)

    async def test_failed_package_requires_explicit_retry_and_resumes_same_package(self) -> None:
        job = await self._job("retry")
        transport = MemoryArchiveTransport()
        service = ArchiveService(self.repo, ArchivePlanner(), transport)
        package = await service.enqueue_job(job)
        assert package is not None
        transport.fail_once_suffix = package.objects[0].remote_relpath
        await service.run_pending_once()
        failed = await self.repo.get_archive_package(package.id)
        assert failed is not None
        self.assertEqual(failed.state, ArchivePackageState.FAILED)

        # Failed packages are not picked up by the periodic worker until the
        # owner explicitly requests retry.
        self.assertEqual(await service.run_pending_once(), 0)
        reset = await service.retry_package(failed.id)
        self.assertEqual(reset.id, package.id)
        self.assertEqual(reset.state, ArchivePackageState.STAGING)
        self.assertEqual(await service.run_pending_once(), 1)
        completed = await self.repo.get_archive_package(package.id)
        assert completed is not None
        self.assertEqual(completed.state, ArchivePackageState.COMMITTED)

    async def test_job_runner_enqueues_archive_before_optional_publish(self) -> None:
        job = await self._job("runner")

        class NeverIngest:
            async def process(self, current):
                raise AssertionError("planned job should not re-enter ingestion")

        class RecordingArchive:
            def __init__(self) -> None:
                self.ids: list[str] = []

            async def enqueue_job(self, current):
                self.ids.append(current.id)
                return None

        archive = RecordingArchive()
        completed = await JobRunner(self.repo, NeverIngest(), None, archive).process(job)
        self.assertEqual(completed.state, JobState.PLANNED)
        self.assertEqual(archive.ids, [job.id])

