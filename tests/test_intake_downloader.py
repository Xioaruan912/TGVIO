from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.job_control import JobCancelRequested, JobControlService, JobHoldRequested
from tgvio.application.media_analyzer import MediaAnalyzer
from tgvio.application.media_downloader import DiskSpaceLowError, JobDownloader
from tgvio.application.orchestrator import JobOrchestrator
from tgvio.application.processor import IngestionProcessor
from tgvio.domain.job import JobState, MediaItem, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class FakeDownloader:
    async def download(self, item: MediaItem, target_dir: Path, progress_callback=None) -> MediaItem:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{item.index}.bin"
        path.write_bytes(b"payload")
        if progress_callback is not None:
            progress_callback(len(b"payload"), len(b"payload"))
        return replace(item, local_path=str(path), size_bytes=path.stat().st_size)


class FakeInspector:
    async def inspect(self, item: MediaItem) -> MediaItem:
        return replace(item, mime_type="application/octet-stream", sha256="abc")


class ReleasableDownloader:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def download(self, item: MediaItem, target_dir: Path, progress_callback=None) -> MediaItem:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{item.index}.bin"
        path.write_bytes(b"payload")
        return replace(item, local_path=str(path), size_bytes=path.stat().st_size)


class BlockingDownloader:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def download(self, item: MediaItem, target_dir: Path, progress_callback=None) -> MediaItem:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class IntakeAndDownloaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = SQLiteJobRepository(self.root / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_intake_creates_durable_album_job(self) -> None:
        service = IntakeService(self.repo)
        job = await service.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.PHOTO,
                    source="telegram:42:100",
                    caption="first",
                    grouped_id=999,
                    source_chat_id=42,
                    source_message_id=100,
                ),
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:101",
                    grouped_id=999,
                    source_chat_id=42,
                    source_message_id=101,
                ),
            ],
        )
        loaded = await self.repo.get(job.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.state, JobState.RECEIVED)
        self.assertEqual([item.index for item in loaded.items], [0, 1])
        self.assertEqual(loaded.items[0].caption, "first")
        self.assertEqual(loaded.items[1].grouped_id, 999)

    async def test_download_persists_each_local_path_and_completes(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.DOCUMENT,
                    source="telegram:42:200",
                    source_chat_id=42,
                    source_message_id=200,
                ),
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:201",
                    source_chat_id=42,
                    source_message_id=201,
                ),
            ],
        )
        downloader = JobDownloader(
            self.repo,
            FakeDownloader(),
            self.root / "downloads",
            reserve_bytes=0,
        )
        completed = await downloader.download(job)
        self.assertEqual(completed.state, JobState.DOWNLOADED)
        self.assertTrue(all(item.local_path for item in completed.items))
        progress = await self.repo.get_job_progress(job.id)
        assert progress is not None
        self.assertEqual(progress.phase, "downloaded")
        self.assertEqual(progress.current, 2)
        self.assertEqual(progress.total, 2)
        events = await self.repo.list_events(job.id)
        self.assertEqual(
            [event.event_type for event in events],
            ["job_created", "download_started", "download_completed"],
        )

    async def test_disk_guard_fails_before_transport(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:300",
                    size_bytes=1,
                    source_chat_id=42,
                    source_message_id=300,
                )
            ],
        )
        downloader = JobDownloader(
            self.repo,
            FakeDownloader(),
            self.root / "downloads",
            reserve_bytes=10**18,
        )
        with self.assertRaises(DiskSpaceLowError):
            await downloader.download(job)
        failed = await self.repo.get(job.id)
        assert failed is not None
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.error_code, "disk_low")

    async def test_concurrent_job_creation_is_serialized(self) -> None:
        intake = IntakeService(self.repo)

        async def create(message_id: int):
            return await intake.accept(
                owner_id=42,
                destination="@channel",
                media=[
                    IncomingMedia(
                        kind=MediaKind.PHOTO,
                        source=f"telegram:42:{message_id}",
                        source_chat_id=42,
                        source_message_id=message_id,
                    )
                ],
            )

        jobs = await asyncio.gather(*(create(i) for i in range(20)))
        self.assertEqual(len({job.id for job in jobs}), 20)
        for job in jobs:
            self.assertIsNotNone(await self.repo.get(job.id))

    async def test_cancel_interrupts_inflight_download_at_safe_local_boundary(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:350",
                    source_chat_id=42,
                    source_message_id=350,
                )
            ],
        )
        control = JobControlService(self.repo)
        transport = BlockingDownloader()
        downloader = JobDownloader(
            self.repo,
            transport,
            self.root / "downloads",
            reserve_bytes=0,
            control=control,
        )
        running = asyncio.create_task(downloader.download(job))
        await asyncio.wait_for(transport.started.wait(), timeout=2)
        current = await self.repo.get(job.id)
        assert current is not None
        await control.request_cancel(current, reason="test cancel")
        with self.assertRaises(JobCancelRequested):
            await asyncio.wait_for(running, timeout=2)
        self.assertTrue(transport.cancelled.is_set())
        cancelled = await self.repo.get(job.id)
        assert cancelled is not None
        self.assertEqual(cancelled.state, JobState.CANCELLED)

    async def test_hold_finishes_current_download_item_then_stops_at_safe_boundary(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:351",
                    source_chat_id=42,
                    source_message_id=351,
                )
            ],
        )
        control = JobControlService(self.repo)
        transport = ReleasableDownloader()
        downloader = JobDownloader(
            self.repo,
            transport,
            self.root / "downloads",
            reserve_bytes=0,
            control=control,
        )
        running = asyncio.create_task(downloader.download(job))
        await asyncio.wait_for(transport.started.wait(), timeout=2)
        current = await self.repo.get(job.id)
        assert current is not None
        await control.request_hold(current, reason="test hold")
        await asyncio.sleep(0.6)
        self.assertFalse(running.done())
        self.assertFalse(transport.cancelled.is_set())

        transport.release.set()
        with self.assertRaises(JobHoldRequested):
            await asyncio.wait_for(running, timeout=2)
        held = await self.repo.get(job.id)
        assert held is not None
        self.assertEqual(held.state, JobState.DOWNLOADING)
        self.assertIsNotNone(held.items[0].local_path)
        self.assertFalse(transport.cancelled.is_set())

        await control.resume(held)
        completed = await downloader.download(held)
        self.assertEqual(completed.state, JobState.DOWNLOADED)

    async def test_ingestion_processor_resumes_downloading_job_after_restart(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.DOCUMENT,
                    source="telegram:42:400",
                    source_chat_id=42,
                    source_message_id=400,
                )
            ],
        )
        job = await self.repo.transition(
            job.id,
            JobState.DOWNLOADING,
            event_type="download_started",
        )
        processor = IngestionProcessor(
            JobDownloader(
                self.repo,
                FakeDownloader(),
                self.root / "downloads",
                reserve_bytes=0,
            ),
            MediaAnalyzer(self.repo, FakeInspector()),
            JobOrchestrator(self.repo),
        )
        completed = await processor.process(job)
        self.assertEqual(completed.state, JobState.PLANNED)
        progress = await self.repo.get_job_progress(job.id)
        assert progress is not None
        self.assertEqual(progress.phase, "planned")
        plan = await self.repo.get_publish_plan(job.id)
        self.assertIsNotNone(plan)
        events = await self.repo.list_events(job.id)
        self.assertEqual(
            [event.event_type for event in events],
            [
                "job_created",
                "download_started",
                "download_completed",
                "analysis_started",
                "analysis_completed",
                "publish_plan_created",
            ],
        )
