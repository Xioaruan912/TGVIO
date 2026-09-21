from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.job_control import JobCancelRequested, JobControlService, JobHoldRequested
from tgvio.application.media_analyzer import MediaAnalyzer
from tgvio.application.media_downloader import DiskSpaceLowError, JobDownloader, classify_download_error
from tgvio.adapters.telegram.media_downloader import TelethonMediaDownloader
from tgvio.application.orchestrator import JobOrchestrator
from tgvio.application.processor import IngestionProcessor
from tgvio.domain.job import (
    DOWNLOAD_SKIPPED_CODE_KEY,
    DOWNLOAD_SKIPPED_KEY,
    JobState,
    MediaItem,
    MediaKind,
    item_download_skipped,
)
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

    async def test_one_bad_item_is_skipped_and_the_rest_completes(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:400",
                    size_bytes=10,
                    source_chat_id=42,
                    source_message_id=400,
                ),
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:401",
                    size_bytes=10,
                    source_chat_id=42,
                    source_message_id=401,
                ),
            ],
        )

        class FlakyDownloader(FakeDownloader):
            async def download(self, item, target_dir, progress_callback=None):
                if item.source_message_id == 400:
                    raise TimeoutError("Timeout while fetching data (caused by GetFileRequest)")
                return await super().download(item, target_dir, progress_callback)

        downloader = JobDownloader(
            self.repo,
            FlakyDownloader(),
            self.root / "downloads",
            reserve_bytes=0,
            item_attempts=2,
            item_retry_delay_seconds=0,
        )
        completed = await downloader.download(job)
        self.assertEqual(completed.state, JobState.DOWNLOADED)
        skipped = [item for item in completed.items if item_download_skipped(item)]
        self.assertEqual([item.index for item in skipped], [0])
        self.assertEqual(
            skipped[0].metadata[DOWNLOAD_SKIPPED_CODE_KEY], "telegram_file_timeout"
        )
        self.assertEqual(completed.policy["download_skipped"][0]["error_code"], "telegram_file_timeout")
        self.assertTrue(completed.items[1].local_path)
        events = await self.repo.list_events(job.id)
        types = [event.event_type for event in events]
        self.assertIn("download_completed", types)

    async def test_tolerance_can_be_disabled_for_all_or_nothing(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:410",
                    size_bytes=10,
                    source_chat_id=42,
                    source_message_id=410,
                )
            ],
        )

        class AlwaysFails(FakeDownloader):
            async def download(self, item, target_dir, progress_callback=None):
                raise TimeoutError("Timeout while fetching data (caused by GetFileRequest)")

        downloader = JobDownloader(
            self.repo,
            AlwaysFails(),
            self.root / "downloads",
            reserve_bytes=0,
            item_attempts=1,
            item_tolerance=False,
        )
        with self.assertRaises(TimeoutError):
            await downloader.download(job)
        failed = await self.repo.get(job.id)
        assert failed is not None
        self.assertEqual(failed.state, JobState.FAILED)

    async def test_successful_retry_clears_old_skip_metadata_and_policy(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:415",
                    size_bytes=10,
                    source_chat_id=42,
                    source_message_id=415,
                    metadata={
                        DOWNLOAD_SKIPPED_KEY: True,
                        DOWNLOAD_SKIPPED_CODE_KEY: "telegram_file_timeout",
                    },
                )
            ],
        )
        job.policy["download_skipped"] = [{"index": 0, "error_code": "telegram_file_timeout"}]
        await self.repo.save(job)

        completed = await JobDownloader(
            self.repo,
            FakeDownloader(),
            self.root / "downloads",
            reserve_bytes=0,
            item_attempts=1,
        ).download(job)

        self.assertFalse(item_download_skipped(completed.items[0]))
        self.assertNotIn(DOWNLOAD_SKIPPED_CODE_KEY, completed.items[0].metadata)
        self.assertNotIn("download_skipped", completed.policy)

    def test_only_telegram_specific_timeouts_get_the_long_retry_code(self) -> None:
        self.assertEqual(classify_download_error(TimeoutError("socket stalled"))[0], "download_failed")
        self.assertEqual(
            classify_download_error(TimeoutError("Timeout while fetching data (GetFileRequest)"))[0],
            "telegram_file_timeout",
        )

    def test_telegram_downloader_success_clears_old_skip_metadata(self) -> None:
        item = MediaItem(
            index=0,
            kind=MediaKind.VIDEO,
            source="telegram:1:2",
            metadata={DOWNLOAD_SKIPPED_KEY: True, DOWNLOAD_SKIPPED_CODE_KEY: "telegram_file_timeout"},
        )
        completed = TelethonMediaDownloader._completed(item, Path("/tmp/item.mp4"), 12, reused=False)
        self.assertFalse(item_download_skipped(completed))
        self.assertNotIn(DOWNLOAD_SKIPPED_CODE_KEY, completed.metadata)

    async def test_every_item_skipped_fails_with_the_telegram_timeout_code(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:420",
                    size_bytes=10,
                    source_chat_id=42,
                    source_message_id=420,
                ),
                IncomingMedia(
                    kind=MediaKind.PHOTO,
                    source="telegram:42:421",
                    size_bytes=10,
                    source_chat_id=42,
                    source_message_id=421,
                ),
            ],
        )

        class AlwaysTimesOut(FakeDownloader):
            async def download(self, item, target_dir, progress_callback=None):
                raise TimeoutError("Timeout while fetching data (caused by GetFileRequest)")

        downloader = JobDownloader(
            self.repo,
            AlwaysTimesOut(),
            self.root / "downloads",
            reserve_bytes=0,
            item_attempts=1,
        )
        with self.assertRaises(Exception):
            await downloader.download(job)
        failed = await self.repo.get(job.id)
        assert failed is not None
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.error_code, "telegram_file_timeout")
        self.assertIn("无法从 Telegram 取用", failed.error_message or "")

    async def test_skipped_items_are_left_out_of_analysis_and_plan(self) -> None:
        intake = IntakeService(self.repo)
        job = await intake.accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:430",
                    size_bytes=10,
                    source_chat_id=42,
                    source_message_id=430,
                ),
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:431",
                    size_bytes=10,
                    source_chat_id=42,
                    source_message_id=431,
                ),
            ],
        )

        class FlakyDownloader(FakeDownloader):
            async def download(self, item, target_dir, progress_callback=None):
                if item.source_message_id == 430:
                    raise TimeoutError("Timeout while fetching data (caused by GetFileRequest)")
                return await super().download(item, target_dir, progress_callback)

        downloader = JobDownloader(
            self.repo,
            FlakyDownloader(),
            self.root / "downloads",
            reserve_bytes=0,
            item_attempts=1,
        )
        downloaded = await downloader.download(job)

        inspected: list[int] = []

        class RecordingInspector(FakeInspector):
            async def inspect(self, item):
                inspected.append(item.index)
                return await super().inspect(item)

        analyzed = await MediaAnalyzer(self.repo, RecordingInspector()).analyze(downloaded)
        self.assertEqual(inspected, [1])

        orchestrator = JobOrchestrator(self.repo)
        plan = await orchestrator.mark_planned(analyzed)
        planned = [index for step in plan.steps for index in step.item_indexes]
        self.assertNotIn(0, planned)
        self.assertIn(1, planned)

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
