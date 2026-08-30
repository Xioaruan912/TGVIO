import asyncio
import os
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from src import bot
from src.repository import SQLiteRepository
from tests.fakes import FakeClient, FakeDownloader, FakeMessage, FakePublisher, FakeStatusMessage


async def cancel_task(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class RepositoryBackedPipelineTests(unittest.IsolatedAsyncioTestCase):
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
        self.pipeline._counter = 2000

    async def asyncTearDown(self) -> None:
        await self.pipeline.job_queue.drain_shadow()
        await self.repo.close()

    async def test_recovery_rebinds_queued_url_and_ready_cache(self) -> None:
        queued = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/recover",
            legacy_seq=2100,
            event_payload={"schema_version": 1},
        )
        ready = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/ready",
            legacy_seq=2101,
            event_payload={"schema_version": 1},
        )
        claim = await self.repo.claim_next_download("prep")
        self.assertEqual(claim.job.id, queued.id)
        current = await self.repo.get_job(queued.id)
        await self.repo.transition_job(
            queued.id,
            expected_revision=current.revision,
            to_state="cancelled",
            event_type="test_skip",
            payload={"schema_version": 1},
        )
        claim = await self.repo.claim_next_download("prep-ready")
        self.assertEqual(claim.job.id, ready.id)
        path = self.downloads / "job-2101" / "media.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"ready")
        await self.repo.record_download_ready(
            ready.id,
            [str(path)],
            expected_revision=claim.job.revision,
        )

        actions = await self.pipeline.recover_from_repository()
        self.assertEqual([action.action for action in actions], ["publish"])
        self.assertIn(2101, self.pipeline.jobs)
        self.assertEqual(self.pipeline.results[2101].result(), str(path.resolve()))
        self.assertEqual(self.pipeline.shadow_state.job_ids[2101], ready.id)

    async def test_repository_claimed_download_completes_to_ready(self) -> None:
        seq = self.pipeline.submit(
            "url",
            FakeStatusMessage(),
            FakeMessage(1),
            url="https://example.invalid/live",
            user_id=42,
        )
        path = self.downloads / f"job-{seq}" / "media.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"payload")

        async def run(job):
            for hook in self.pipeline.downloader.pre_download_hooks:
                await hook(job)
            for hook in self.pipeline.downloader.post_download_hooks:
                await hook(job, str(path))
            return str(path)

        self.pipeline.downloader.run = run
        worker = asyncio.create_task(self.pipeline._download_worker())
        await asyncio.wait_for(self.pipeline.input_q.join(), timeout=1)
        await cancel_task(worker)
        await self.pipeline.job_queue.drain_shadow()
        job_id = self.pipeline.shadow_state.job_ids[seq]
        current = await self.repo.get_job(job_id)
        self.assertEqual(current.state, "ready")
        self.assertTrue(self.pipeline.results[seq].done())
        items = await self.repo.list_job_items(job_id)
        self.assertEqual([item.local_path for item in items], [str(path.resolve())])

    async def test_repository_claimed_download_failure_settles_failed_without_publisher(self) -> None:
        seq = self.pipeline.submit(
            "url",
            FakeStatusMessage(),
            FakeMessage(2),
            url="https://example.invalid/fail",
            user_id=42,
        )
        self.pipeline.downloader.failures[seq] = RuntimeError("boom")
        worker = asyncio.create_task(self.pipeline._download_worker())
        await asyncio.wait_for(self.pipeline.input_q.join(), timeout=1)
        await cancel_task(worker)
        await self.pipeline.job_queue.drain_shadow()
        job_id = self.pipeline.shadow_state.job_ids[seq]
        current = await self.repo.get_job(job_id)
        self.assertEqual(current.state, "failed")
        self.assertIn(seq, self.pipeline.retryable)
        self.assertNotIn(seq, self.pipeline.active_seqs)

