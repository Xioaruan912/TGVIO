from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.archive_planner import ArchivePlanner
from tgvio.domain.archive import ArchiveObjectState, ArchivePackageState
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class ArchiveRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = SQLiteJobRepository(self.root / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _job(self) -> Job:
        media = self.root / "movie.mp4"
        media.write_bytes(b"movie-payload")
        job = Job(
            id="3" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.RECEIVED,
            created_at="2026-09-04 01:19:23",
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.VIDEO,
                    source="fixture",
                    local_path=str(media),
                    name="movie.mp4",
                    size_bytes=media.stat().st_size,
                    sha256="c" * 64,
                )
            ],
        )
        await self.repo.create(job)
        return job

    async def test_archive_plan_round_trips_as_one_durable_package(self) -> None:
        job = await self._job()
        plan = ArchivePlanner().plan(job)
        saved = await self.repo.save_archive_plan(plan)
        self.assertEqual(saved.state, ArchivePackageState.PLANNED)
        self.assertEqual(saved.remote_path, plan.package.remote_path)
        self.assertEqual(len(saved.objects), 1)
        self.assertEqual(saved.objects[0].remote_relpath, "media/001__movie.mp4")
        self.assertEqual(saved.manifest["schema"], "tgvio.archive/v1")
        events = await self.repo.list_archive_events(saved.id)
        self.assertEqual([event.event_type for event in events], ["archive_planned"])
        self.assertEqual(events[0].detail["media_total"], 1)

    async def test_replanning_while_planned_is_idempotent_not_attempt_sprawl(self) -> None:
        job = await self._job()
        plan = ArchivePlanner().plan(job)
        first = await self.repo.save_archive_plan(plan)
        second = await self.repo.save_archive_plan(plan)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(second.objects), 1)
        events = await self.repo.list_archive_events(second.id)
        self.assertEqual(len(events), 1)

    async def test_archive_state_transitions_are_durable_and_auditable(self) -> None:
        job = await self._job()
        package = await self.repo.save_archive_plan(ArchivePlanner().plan(job))
        package = await self.repo.update_archive_package_state(
            package.id,
            ArchivePackageState.STAGING,
            event_type="archive_staging_started",
        )
        package = await self.repo.update_archive_package_state(
            package.id,
            ArchivePackageState.UPLOADING,
            event_type="archive_upload_started",
        )
        obj = package.objects[0]
        assert obj.id is not None
        obj = await self.repo.update_archive_object_state(
            obj.id,
            ArchiveObjectState.UPLOADING,
            event_type="archive_object_upload_started",
        )
        obj = await self.repo.update_archive_object_state(
            obj.id,
            ArchiveObjectState.STORED,
            event_type="archive_object_stored",
            verification_method="size",
            remote_etag='"abc"',
        )
        self.assertEqual(obj.state, ArchiveObjectState.STORED)
        self.assertEqual(obj.verification_method, "size")
        self.assertEqual(obj.remote_etag, '"abc"')
        package = await self.repo.update_archive_package_state(
            package.id,
            ArchivePackageState.VERIFYING,
            event_type="archive_verification_started",
        )
        package = await self.repo.update_archive_package_state(
            package.id,
            ArchivePackageState.COMMITTED,
            event_type="archive_committed",
            committed=True,
        )
        self.assertEqual(package.state, ArchivePackageState.COMMITTED)
        self.assertIsNotNone(package.committed_at)
        events = await self.repo.list_archive_events(package.id)
        self.assertEqual(
            [event.event_type for event in events],
            [
                "archive_planned",
                "archive_staging_started",
                "archive_upload_started",
                "archive_object_upload_started",
                "archive_object_stored",
                "archive_verification_started",
                "archive_committed",
            ],
        )

    async def test_illegal_archive_transition_fails_closed(self) -> None:
        job = await self._job()
        package = await self.repo.save_archive_plan(ArchivePlanner().plan(job))
        with self.assertRaisesRegex(ValueError, "illegal archive package transition"):
            await self.repo.update_archive_package_state(
                package.id,
                ArchivePackageState.COMMITTED,
                event_type="invalid_shortcut",
                committed=True,
            )
        loaded = await self.repo.get_archive_package(package.id)
        assert loaded is not None
        self.assertEqual(loaded.state, ArchivePackageState.PLANNED)

