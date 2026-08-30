import asyncio
import os
import tempfile
import unittest
from contextlib import suppress
from unittest.mock import patch

from src import bot
from src.models import Job, RetryInfo
from tests.fakes import (
    FakeCallbackEvent,
    FakeClient,
    FakeDownloader,
    FakeMessage,
    FakePublisher,
    FakeStatusMessage,
)


async def wait_until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


async def cancel_task(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class PipelineBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        state_dir = os.path.join(self.tempdir.name, "session")
        download_dir = os.path.join(self.tempdir.name, "downloads")
        os.makedirs(state_dir)
        os.makedirs(download_dir)

        replacements = {
            "PREFS_FILE": os.path.join(state_dir, "prefs.json"),
            "LEGACY_PREFS_FILE": os.path.join(state_dir, "legacy-prefs.json"),
            "WEBDAV_CFG_FILE": os.path.join(state_dir, "webdav.json"),
            "WEBDAV_LOGS_FILE": os.path.join(state_dir, "webdav-logs.json"),
            "WEBDAV_COUNT_FILE": os.path.join(state_dir, "webdav-count.json"),
            "PROXY_FILE": os.path.join(state_dir, "proxy.json"),
            "DOWNLOAD_DIR": download_dir,
            "ALLOWED_USERS": {42},
            "MediaDownloader": FakeDownloader,
            "MediaPublisher": FakePublisher,
        }
        self.patchers = [patch.object(bot, key, value) for key, value in replacements.items()]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = FakeClient()
        self.pipeline = bot._Pipeline(self.client)
        self.pipeline._counter = 100

    def make_job(self, seq: int, path: str = "") -> Job:
        return Job(
            seq=seq,
            kind="media",
            status=FakeStatusMessage(),
            message=FakeMessage(seq),
            user_id=42,
            cached_path=path,
        )

    def register_callback_pipeline(self) -> tuple[bot._Pipeline, FakeClient]:
        client = FakeClient()
        with patch.object(bot._Pipeline, "start", autospec=True) as start:
            pipeline = bot.register_handlers(client)
            start.assert_called_once_with(pipeline)
        pipeline._counter = 500
        return pipeline, client

    async def test_submit_enqueues_jobs_and_positions_follow_accept_order(self) -> None:
        first = self.pipeline.submit(
            "media", FakeStatusMessage(), FakeMessage(1), user_id=42
        )
        second = self.pipeline.submit(
            "url",
            FakeStatusMessage(),
            FakeMessage(2),
            url="https://example.invalid/video",
            user_id=42,
        )

        self.assertEqual((first, second), (100, 101))
        self.assertEqual(self.pipeline.active_seqs, {100, 101})
        self.assertEqual(self.pipeline.task_label(first), "队列第 1 位")
        self.assertEqual(self.pipeline.task_label(second), "队列第 2 位")

        first_job = self.pipeline.input_q.get_nowait()
        second_job = self.pipeline.input_q.get_nowait()
        self.pipeline.input_q.task_done()
        self.pipeline.input_q.task_done()
        self.assertEqual((first_job.seq, second_job.seq), (100, 101))
        self.assertEqual(second_job.url, "https://example.invalid/video")

    async def test_pending_cancel_is_idempotent_and_settles_sequence(self) -> None:
        status = FakeStatusMessage("confirm")
        self.pipeline.register_pending(
            110, "media", message=FakeMessage(1), user_id=42
        )
        self.pipeline.set_pending_status(110, status)
        timeout_task = self.pipeline.pending[110].timeout_task

        self.assertTrue(await self.pipeline._cancel_pending(110))
        self.assertFalse(await self.pipeline._cancel_pending(110))
        self.assertNotIn(110, self.pipeline.pending)
        self.assertTrue(status.deleted)
        self.assertIs(self.pipeline.results[110].result(), bot._CANCELLED)

        with suppress(asyncio.CancelledError):
            await timeout_task

    async def test_queued_cancel_skips_downloader_when_worker_claims_job(self) -> None:
        job = self.make_job(120)
        self.pipeline.enqueue(job)

        self.assertTrue(await self.pipeline._cancel_seq(120))
        worker = asyncio.create_task(self.pipeline._download_worker())
        await asyncio.wait_for(self.pipeline.input_q.join(), timeout=1)
        await cancel_task(worker)

        self.assertEqual(self.pipeline.downloader.calls, [])
        self.assertTrue(job.status.deleted)
        self.assertNotIn(120, self.pipeline._cancel_marked)
        self.assertIs(self.pipeline.results[120].result(), bot._CANCELLED)
        self.pipeline._finish_seq(120)

    async def test_paused_ready_job_is_skipped_until_resumed(self) -> None:
        for seq in (131, 130):
            self.pipeline.jobs[seq] = self.make_job(seq)
            self.pipeline.active_seqs.add(seq)
            self.pipeline._set_result(seq, f"/fake/{seq}.mp4")

        self.pipeline._paused_files.add(130)
        self.assertEqual(self.pipeline._pick_next_upload(), 131)

        self.pipeline._paused_files.discard(130)
        self.assertEqual(self.pipeline._pick_next_upload(), 130)

        self.pipeline._finish_seq(130)
        self.pipeline._finish_seq(131)

    async def test_upload_worker_publishes_ready_jobs_in_sequence_order(self) -> None:
        self.pipeline.publisher.expected_calls = 2
        self.pipeline._schedule_cleanup = lambda _seq, _cleanup: None
        for seq in (141, 140):
            self.pipeline.jobs[seq] = self.make_job(seq)
            self.pipeline.active_seqs.add(seq)
            self.pipeline._set_result(seq, f"/fake/{seq}.mp4")

        worker = asyncio.create_task(self.pipeline._upload_worker())
        await asyncio.wait_for(self.pipeline.publisher.completed.wait(), timeout=1)
        await wait_until(lambda: not self.pipeline.results)
        await cancel_task(worker)

        self.assertEqual(self.pipeline.publisher.calls, [140, 141])
        self.assertFalse(self.pipeline.jobs)
        self.assertFalse(self.pipeline.active_seqs)

    async def test_upload_failure_preserves_cache_and_registers_cached_retry(self) -> None:
        seq = 150
        workdir = self.pipeline._workdir(seq)
        os.makedirs(workdir)
        path = os.path.join(workdir, "video.mp4")
        with open(path, "wb") as media_file:
            media_file.write(b"media")

        job = self.make_job(seq)
        self.pipeline.jobs[seq] = job
        self.pipeline.active_seqs.add(seq)
        self.pipeline._set_result(seq, path)
        self.pipeline.publisher.failures[seq] = RuntimeError("publish unavailable")

        with self.assertLogs("src.bot", level="ERROR"):
            worker = asyncio.create_task(self.pipeline._upload_worker())
            await wait_until(lambda: seq not in self.pipeline.results)
        await cancel_task(worker)

        self.assertTrue(os.path.isfile(path))
        self.assertEqual(self.pipeline.retryable[seq].path, path)
        self.assertIn("上传失败", job.status.text)
        self.assertNotIn(seq, self.pipeline.active_seqs)

    async def test_webdav_failure_mark_prevents_delayed_cache_cleanup(self) -> None:
        seq = 160
        workdir = self.pipeline._workdir(seq)
        os.makedirs(workdir)
        path = os.path.join(workdir, "video.mp4")
        with open(path, "wb") as media_file:
            media_file.write(b"media")

        release = asyncio.Event()
        webdav_task = asyncio.create_task(release.wait())
        self.pipeline._webdav_tasks[webdav_task] = seq
        self.pipeline._schedule_cleanup(seq, "")
        await asyncio.sleep(0)
        self.assertTrue(os.path.isfile(path))

        self.pipeline.webdav_keep_cache.add(seq)
        release.set()
        await webdav_task
        await asyncio.sleep(0.01)

        self.assertTrue(os.path.isfile(path))

    async def test_retry_callback_consumes_retry_once_and_reuses_cache(self) -> None:
        pipeline, client = self.register_callback_pipeline()
        cached_path = os.path.join(self.tempdir.name, "downloads", "job-7", "video.mp4")
        old_job = self.make_job(7)
        pipeline.retryable[7] = RetryInfo(job=old_job, path=cached_path)
        callback = client.handlers["on_callback"]

        first_event = FakeCallbackEvent(client, b"retry:7")
        await callback(first_event)
        new_job = pipeline.input_q.get_nowait()
        pipeline.input_q.task_done()

        self.assertEqual(new_job.seq, 500)
        self.assertEqual(new_job.cached_path, cached_path)
        self.assertEqual(new_job.cleanup_extra, os.path.dirname(cached_path))
        self.assertEqual(first_event.answers[-1], "已重新入队")

        second_event = FakeCallbackEvent(client, b"retry:7")
        await callback(second_event)
        self.assertEqual(second_event.answers[-1], "该任务已失效（可能已重试）")
        self.assertTrue(pipeline.input_q.empty())

    async def test_pause_resume_callbacks_update_job_and_global_state(self) -> None:
        pipeline, client = self.register_callback_pipeline()
        callback = client.handlers["on_callback"]
        job = self.make_job(20)
        pipeline.jobs[20] = job
        pipeline.active_seqs.add(20)

        hold = FakeCallbackEvent(client, b"hold:20")
        await callback(hold)
        self.assertIn(20, pipeline._paused_files)
        self.assertIn("已暂停", job.status.text)

        resume = FakeCallbackEvent(client, b"resume:20")
        await callback(resume)
        self.assertNotIn(20, pipeline._paused_files)
        self.assertIn("已继续", job.status.text)

        await callback(FakeCallbackEvent(client, b"q_pause"))
        self.assertTrue(pipeline._paused)
        await callback(FakeCallbackEvent(client, b"q_resume"))
        self.assertFalse(pipeline._paused)

    async def test_confirmation_callback_consumes_pending_once(self) -> None:
        pipeline, client = self.register_callback_pipeline()
        callback = client.handlers["on_callback"]
        pipeline.register_pending(
            30, "media", message=FakeMessage(30), user_id=42
        )
        pending_status = FakeStatusMessage("confirm")
        pipeline.set_pending_status(30, pending_status)
        timeout_task = pipeline.pending[30].timeout_task

        first_event = FakeCallbackEvent(client, b"confirm:30:1")
        await callback(first_event)
        queued = pipeline.input_q.get_nowait()
        pipeline.input_q.task_done()
        self.assertEqual(queued.seq, 30)
        self.assertTrue(queued.spoiler)
        self.assertEqual(first_event.answers[-1], "已确认")

        second_event = FakeCallbackEvent(client, b"confirm:30:1")
        await callback(second_event)
        self.assertEqual(second_event.answers[-1], "该确认已失效")
        self.assertTrue(pipeline.input_q.empty())

        with suppress(asyncio.CancelledError):
            await timeout_task

    async def test_album_auto_enqueue_merges_unstarted_batches(self) -> None:
        first_batch = [FakeMessage(1), FakeMessage(2)]
        second_batch = [FakeMessage(2), FakeMessage(3)]

        first_seq = await self.pipeline._auto_enqueue(
            "album", None, first_batch, user_id=42
        )
        second_seq = await self.pipeline._auto_enqueue(
            "album", None, second_batch, user_id=42
        )

        self.assertEqual(first_seq, second_seq)
        self.assertEqual(self.pipeline.input_q.qsize(), 1)
        queued = self.pipeline.input_q.get_nowait()
        self.pipeline.input_q.task_done()
        self.assertEqual([message.id for message in queued.album], [1, 2, 3])
        self.assertEqual(self.pipeline.pending_albums[42], first_seq)
        self.assertIn("共 3 条", queued.status.text)

    async def test_collection_session_flattens_batches_and_preserves_texts(self) -> None:
        await self.pipeline._session_add_batch(42, [FakeMessage(1)], 42)
        session_status = self.pipeline.sessions[42].status
        await self.pipeline._session_add_batch(
            42, [FakeMessage(2), FakeMessage(3)], 42
        )
        await self.pipeline._session_add_text(42, "第一行", 42)
        await self.pipeline._session_add_text(42, "第二行", 42)

        self.assertEqual(len(self.client.sent_messages), 1)
        count = await self.pipeline._session_finalize(42, 42)
        queued = self.pipeline.input_q.get_nowait()
        self.pipeline.input_q.task_done()

        self.assertEqual(count, 3)
        self.assertEqual(queued.kind, "collection")
        self.assertEqual([message.id for message in queued.album], [1, 2, 3])
        self.assertEqual(queued.texts, ["第一行", "第二行"])
        self.assertNotIn(42, self.pipeline.sessions)
        self.assertIn("已结束收集", session_status.text)

    async def test_ask_mode_session_finalize_creates_pending_collection(self) -> None:
        self.pipeline.set_spoiler_mode(42, "ask")
        await self.pipeline._session_add_batch(
            42, [FakeMessage(1), FakeMessage(2)], 42
        )
        await self.pipeline._session_add_text(42, "合集说明", 42)

        count = await self.pipeline._session_finalize(42, 42)

        self.assertEqual(count, 2)
        self.assertTrue(self.pipeline.input_q.empty())
        self.assertEqual(len(self.pipeline.pending), 1)
        pending = next(iter(self.pipeline.pending.values()))
        self.assertEqual(pending.kind, "collection")
        self.assertEqual([message.id for message in pending.album], [1, 2])
        self.assertEqual(pending.texts, ["合集说明"])
        timeout_task = pending.timeout_task
        await self.pipeline._cancel_pending(pending.seq)
        with suppress(asyncio.CancelledError):
            await timeout_task

    async def test_spoiler_preference_and_force_normal_are_copied_to_jobs(self) -> None:
        self.pipeline.set_spoiler_mode(42, "always_spoiler")
        await self.pipeline._auto_enqueue(
            "media", FakeMessage(1), None, user_id=42
        )
        await self.pipeline._auto_enqueue(
            "media", FakeMessage(2), None, user_id=42, force_normal=True
        )

        spoiler_job = self.pipeline.input_q.get_nowait()
        normal_job = self.pipeline.input_q.get_nowait()
        self.pipeline.input_q.task_done()
        self.pipeline.input_q.task_done()
        self.assertTrue(spoiler_job.spoiler)
        self.assertFalse(normal_job.spoiler)

    async def test_confirmation_timeout_reuses_reserved_sequence(self) -> None:
        status = FakeStatusMessage("confirm")
        with patch.object(bot, "CONFIRM_TIMEOUT", 0):
            seq = self.pipeline.reserve_seq()
            self.pipeline.register_pending(
                seq, "media", message=FakeMessage(1), user_id=42
            )
            self.pipeline.set_pending_status(seq, status)
            timeout_task = self.pipeline.pending[seq].timeout_task
            await timeout_task

        queued = self.pipeline.input_q.get_nowait()
        self.pipeline.input_q.task_done()
        self.assertEqual(queued.seq, seq)
        self.assertFalse(queued.spoiler)
        self.assertEqual(self.pipeline.active_seqs, {seq})
        self.assertTrue(status.deleted)

    async def test_two_download_workers_run_jobs_concurrently(self) -> None:
        first_release = asyncio.Event()
        second_release = asyncio.Event()
        self.pipeline.downloader.expected_calls = 2
        self.pipeline.downloader.blockers = {
            170: first_release,
            171: second_release,
        }
        self.pipeline.enqueue(self.make_job(170))
        self.pipeline.enqueue(self.make_job(171))

        workers = [
            asyncio.create_task(self.pipeline._download_worker()),
            asyncio.create_task(self.pipeline._download_worker()),
        ]
        await asyncio.wait_for(self.pipeline.downloader.started.wait(), timeout=1)
        self.assertEqual(set(self.pipeline.downloader.calls), {170, 171})
        self.assertEqual(self.pipeline._active_downloads, 2)

        first_release.set()
        second_release.set()
        await asyncio.wait_for(self.pipeline.input_q.join(), timeout=1)
        for worker in workers:
            await cancel_task(worker)

        self.assertEqual(self.pipeline._active_downloads, 0)
        self.assertTrue(self.pipeline.results[170].done())
        self.assertTrue(self.pipeline.results[171].done())
        self.pipeline._finish_seq(170)
        self.pipeline._finish_seq(171)

    async def test_running_download_cancel_stops_task_and_settles_job(self) -> None:
        release = asyncio.Event()
        self.pipeline.downloader.expected_calls = 1
        self.pipeline.downloader.blockers[180] = release
        job = self.make_job(180)
        self.pipeline.enqueue(job)

        worker = asyncio.create_task(self.pipeline._download_worker())
        await asyncio.wait_for(self.pipeline.downloader.started.wait(), timeout=1)
        self.assertTrue(await self.pipeline._cancel_seq(180))
        await asyncio.wait_for(self.pipeline.input_q.join(), timeout=1)
        await cancel_task(worker)

        self.assertIs(self.pipeline.results[180].result(), bot._CANCELLED)
        self.assertTrue(job.status.deleted)
        self.assertNotIn(180, self.pipeline._download_tasks)
        self.pipeline._finish_seq(180)

    async def test_download_failure_settles_future_without_blocking_worker(self) -> None:
        self.pipeline.downloader.failures[190] = ValueError("bad media")
        self.pipeline.enqueue(self.make_job(190))

        with self.assertLogs("src.bot", level="ERROR"):
            worker = asyncio.create_task(self.pipeline._download_worker())
            await asyncio.wait_for(self.pipeline.input_q.join(), timeout=1)
        await cancel_task(worker)

        failure = self.pipeline.results[190].exception()
        self.assertIsInstance(failure, ValueError)
        self.assertEqual(str(failure), "bad media")
        self.assertEqual(self.pipeline._active_downloads, 0)
        self.pipeline._finish_seq(190)


if __name__ == "__main__":
    unittest.main()
