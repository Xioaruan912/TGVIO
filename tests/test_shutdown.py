import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src import bot
from src.repository import SQLiteRepository
from tests.fakes import FakeClient, FakeDownloader, FakeMessage, FakePublisher, FakeStatusMessage


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


class GracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.downloads = root / "downloads"
        self.downloads.mkdir()
        state = root / "session"
        state.mkdir()
        replacements = {
            "PREFS_FILE": str(state / "prefs.json"),
            "LEGACY_PREFS_FILE": str(state / "legacy-prefs.json"),
            "WEBDAV_CFG_FILE": str(state / "webdav.json"),
            "WEBDAV_LOGS_FILE": str(state / "webdav-logs.json"),
            "WEBDAV_COUNT_FILE": str(state / "webdav-count.json"),
            "PROXY_FILE": str(state / "proxy.json"),
            "DOWNLOAD_DIR": str(self.downloads),
            "AUTO_DELETE_SECONDS": 0,
            "ALLOWED_USERS": {42},
            "MediaDownloader": FakeDownloader,
            "MediaPublisher": FakePublisher,
        }
        self.patchers = [patch.object(bot, key, value) for key, value in replacements.items()]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.repo = SQLiteRepository(root / "state.sqlite3", download_root=self.downloads)
        await self.repo.open()
        await self.repo.migrate()
        self.client = FakeClient()
        self.pipeline = bot.register_handlers(
            self.client,
            repository=self.repo,
            start_workers=False,
        )
        self.pipeline._counter = 3000

    async def asyncTearDown(self) -> None:
        await self.pipeline.shutdown(timeout=0)
        await self.repo.close()

    async def test_idle_and_repeated_shutdown_are_idempotent(self) -> None:
        source_runtime = type("_SourceRuntime", (), {"close": AsyncMock()})()
        self.pipeline.source_runtime = source_runtime
        self.pipeline.start()
        await self.pipeline.shutdown(timeout=0)
        await self.pipeline.shutdown(timeout=0)
        source_runtime.close.assert_awaited_once()
        self.assertTrue(self.pipeline._stopping)
        self.assertTrue(self.pipeline._shutdown_complete)
        self.assertFalse([task for task in self.pipeline._worker_tasks if not task.done()])
        with self.assertRaisesRegex(RuntimeError, "shutting down"):
            self.pipeline.enqueue(
                bot._Job(
                    seq=3999,
                    kind="url",
                    status=FakeStatusMessage(),
                    message=FakeMessage(1),
                    url="https://example.invalid/new",
                    user_id=42,
                )
            )

    async def test_download_claim_is_interrupted_before_transport_cancel_and_recovers(self) -> None:
        seq = self.pipeline.submit(
            "url",
            FakeStatusMessage(),
            FakeMessage(1),
            url="https://example.invalid/shutdown-download",
            user_id=42,
        )
        blocker = asyncio.Event()
        self.pipeline.downloader.blockers[seq] = blocker
        self.pipeline.downloader.expected_calls = 1
        self.pipeline.start()
        await asyncio.wait_for(self.pipeline.downloader.started.wait(), timeout=1)
        await wait_until(lambda: ("download", seq) in self.pipeline._repo_claim_owners)
        status = self.pipeline._runtime_jobs[seq].status
        job_id = self.pipeline.shadow_state.job_ids[seq]

        await self.pipeline.shutdown(timeout=0)
        current = await self.repo.get_job(job_id)
        self.assertEqual(current.state, "interrupted")
        self.assertEqual(status.delete_calls, 0)
        events = await self.repo.list_job_events(job_id)
        self.assertEqual(events[-1].event_type, "shutdown_interrupted")

        recovered = bot.register_handlers(
            self.client,
            repository=self.repo,
            start_workers=False,
        )
        actions = await recovered.recover_from_repository()
        self.assertEqual([(a.action, a.job.id) for a in actions], [("download", job_id)])
        self.assertEqual((await self.repo.get_job(job_id)).state, "queued")
        await recovered.shutdown(timeout=0)

    async def test_publish_claim_is_interrupted_and_complete_cache_recovers(self) -> None:
        path = self.downloads / "job-3100" / "media.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"payload")
        record = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/shutdown-publish",
            legacy_seq=3100,
            event_payload={"schema_version": 1},
        )
        claim = await self.repo.claim_next_download("prepare")
        await self.repo.record_download_ready(
            record.id,
            [str(path)],
            expected_revision=claim.job.revision,
        )
        await self.pipeline.recover_from_repository()
        blocker = asyncio.Event()
        self.pipeline.publisher.blockers[3100] = blocker
        self.pipeline.publisher.expected_starts = 1
        self.pipeline.start()
        await asyncio.wait_for(self.pipeline.publisher.started.wait(), timeout=2)
        await wait_until(lambda: ("publish", 3100) in self.pipeline._repo_claim_owners)
        status = self.pipeline.jobs[3100].status

        await self.pipeline.shutdown(timeout=0)
        self.assertEqual((await self.repo.get_job(record.id)).state, "interrupted")
        self.assertEqual(status.delete_calls, 0)

        recovered = bot.register_handlers(
            self.client,
            repository=self.repo,
            start_workers=False,
        )
        actions = await recovered.recover_from_repository()
        self.assertEqual([(a.action, a.job.id) for a in actions], [("publish", record.id)])
        self.assertEqual((await self.repo.get_job(record.id)).state, "ready")
        await recovered.shutdown(timeout=0)

    async def test_webdav_background_task_is_cancelled_after_bounded_wait(self) -> None:
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(forever())
        self.pipeline._webdav_tasks[task] = 1
        await started.wait()
        await self.pipeline.shutdown(timeout=0)
        self.assertTrue(task.cancelled())
