import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from src import bot
from src.repository import SQLiteRepository
from src.services import JobQueue, OperationStore
from src.views import (
    DurableQueueItemView,
    DurableQueuePageView,
    FailureItemView,
    JobDetailViewState,
    batch_actions_view,
    confirmation_view,
    durable_queue_view,
    failure_center_view,
    job_detail_view,
)
from tests.fakes import FakeCallbackEvent, FakeClient, FakeDownloader, FakePublisher


def callbacks(buttons):
    return [button.data for row in buttons for button in row]


class OperationStoreTests(unittest.TestCase):
    def test_tokens_are_user_scoped_single_use_and_expire(self) -> None:
        store = OperationStore(ttl_seconds=5)
        with patch("src.services.operations.time.time", return_value=100.0):
            item = store.create(user_id=42, action="cancel", job_id=7, expected_revision=3)
        with patch("src.services.operations.time.time", return_value=103.0):
            self.assertIsNone(store.peek(item.operation_id, user_id=99))
            self.assertEqual(store.consume(item.operation_id, user_id=42), item)
            self.assertIsNone(store.consume(item.operation_id, user_id=42))

        with patch("src.services.operations.time.time", return_value=200.0):
            expired = store.create_batch(
                user_id=42,
                action="batch_cancel",
                targets=[(1, 2), (3, 4)],
                total_bytes=123,
            )
        with patch("src.services.operations.time.time", return_value=206.0):
            self.assertIsNone(store.peek(expired.operation_id, user_id=42))


class U2RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.download_root = root / "downloads"
        self.download_root.mkdir()
        self.repo = SQLiteRepository(root / "state.sqlite3", download_root=self.download_root)
        await self.repo.open()
        await self.repo.migrate()
        self.addAsyncCleanup(self.repo.close)

    async def test_sql_pagination_filters_one_hundred_jobs(self) -> None:
        now = time.time()
        for index in range(100):
            state = "failed" if index % 10 == 0 else "queued"
            await self.repo.accept_job(
                kind="url",
                user_id=42,
                state=state,
                source_kind="url",
                source_url=f"https://example.invalid/{index}",
                legacy_seq=1000 + index,
                event_payload={"schema_version": 1},
                now=now + index / 1000,
            )
        page, total = await self.repo.page_jobs(
            user_id=42, filter_name="all", page=0, page_size=5, completed_since=now - 86400
        )
        self.assertEqual(total, 100)
        self.assertEqual(len(page), 5)
        self.assertGreater(page[0]["id"], page[-1]["id"])
        failed, failed_total = await self.repo.page_jobs(
            user_id=42, filter_name="failed", page=1, page_size=5
        )
        self.assertEqual(failed_total, 10)
        self.assertEqual(len(failed), 5)
        self.assertTrue(all(row["state"] == "failed" for row in failed))

    async def test_completed_filter_excludes_old_rows(self) -> None:
        now = time.time()
        old = await self.repo.accept_job(
            kind="url", user_id=42, state="succeeded", source_kind="url",
            event_payload={"schema_version": 1}, now=now - 3 * 86400,
        )
        fresh = await self.repo.accept_job(
            kind="url", user_id=42, state="succeeded", source_kind="url",
            event_payload={"schema_version": 1}, now=now,
        )
        rows, total = await self.repo.page_jobs(
            user_id=42, filter_name="completed", page=0, page_size=5, completed_since=now - 86400
        )
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], fresh.id)
        self.assertNotEqual(rows[0]["id"], old.id)

    async def test_detail_and_failed_cache_cas(self) -> None:
        job_dir = self.download_root / "job-1"
        job_dir.mkdir()
        path = job_dir / "video.mp4"
        path.write_bytes(b"12345")
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="failed",
            source_kind="url",
            legacy_seq=77,
            items=[{"local_path": str(path), "size_bytes": 5, "metadata": {"schema_version": 1}}],
            event_payload={"schema_version": 1},
        )
        pipeline = SimpleNamespace(repository=self.repo, download_dir=str(self.download_root), retryable={})
        queue = JobQueue(pipeline)
        detail = await queue.durable_job_detail(42, job.id)
        self.assertTrue(detail["cache_exists"])
        self.assertNotIn(str(self.download_root), str(detail.get("backup_summary") or ""))
        self.assertEqual(
            await queue.delete_failed_cache_by_id(42, job.id, job.revision + 1), "stale"
        )
        self.assertTrue(path.exists())
        self.assertEqual(await queue.delete_failed_cache_by_id(42, job.id, job.revision), "ok")
        self.assertFalse(path.exists())
        refreshed = await self.repo.job_detail(job.id, user_id=42)
        self.assertIsNone(refreshed["items"][0]["local_path"])

    async def test_failure_message_is_sanitized(self) -> None:
        job = await self.repo.accept_job(
            kind="url", user_id=42, state="queued", source_kind="url",
            event_payload={"schema_version": 1},
        )
        claimed = await self.repo.claim_next_download("test")
        self.assertEqual(claimed.job.id, job.id)
        result = await self.repo.transition_job(
            job.id,
            expected_revision=claimed.job.revision,
            to_state="failed",
            event_type="failed",
            payload={
                "schema_version": 1,
                "error_message": "https://user:secret@example.invalid/x Authorization: bearer-secret token=abc",
            },
        )
        self.assertTrue(result.applied)
        detail = await self.repo.job_detail(job.id, user_id=42)
        message = detail["error_message"]
        self.assertNotIn("secret", message)
        self.assertNotIn("bearer-secret", message)
        self.assertNotIn("token=abc", message)


class U2ViewTests(unittest.TestCase):
    def test_queue_detail_failure_batch_and_confirmation_callbacks_fit(self) -> None:
        queue_text, queue_buttons = durable_queue_view(
            DurableQueuePageView(
                filter_name="all", page=0, pages=20, total=100,
                running=2, waiting=90, paused=1, failed=7,
                items=tuple(
                    DurableQueueItemView(job_id=i, legacy_seq=100 + i, state="queued", total_items=2)
                    for i in range(1, 6)
                ),
            )
        )
        self.assertIn("第 1/20 页", queue_text)
        self.assertEqual(sum(1 for data in callbacks(queue_buttons) if data.startswith(b"j:v:")), 5)
        detail_text, detail_buttons = job_detail_view(
            JobDetailViewState(
                job_id=9, legacy_seq=109, revision=4, kind="url", state="failed",
                source_kind="url", item_count=1, item_bytes=1024, bytes_done=0,
                bytes_total=0, retry_count=1, cache_exists=True, published_count=2,
                backup_state="failed", error_message="network timeout", can_retry=True,
            )
        )
        self.assertIn("network timeout", detail_text)
        self.assertIn(b"j:d:9:4", callbacks(detail_buttons))
        failure_text, failure_buttons = failure_center_view(
            (FailureItemView(9, 109, 4, True, "network timeout", True),), page=0, pages=1
        )
        self.assertIn("失败中心", failure_text)
        batch_text, batch_buttons = batch_actions_view(waiting_count=8, failed_count=2, failed_bytes=2048)
        self.assertIn("等待任务：8", batch_text)
        confirm_text, confirm_buttons = confirmation_view(
            operation_id=123456789, action="batch_cancel", label="8 个等待任务"
        )
        self.assertIn("5 分钟", confirm_text)
        all_data = callbacks(queue_buttons) + callbacks(detail_buttons) + callbacks(failure_buttons) + callbacks(batch_buttons) + callbacks(confirm_buttons)
        self.assertTrue(all(len(data) <= 64 for data in all_data))


class U2HandlerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.download_root = root / "downloads"
        self.state_root = root / "session"
        self.download_root.mkdir()
        self.state_root.mkdir()
        self.repo = SQLiteRepository(root / "state.sqlite3", download_root=self.download_root)
        await self.repo.open()
        await self.repo.migrate()
        self.addAsyncCleanup(self.repo.close)
        replacements = {
            "PREFS_FILE": str(self.state_root / "prefs.json"),
            "LEGACY_PREFS_FILE": str(self.state_root / "legacy-prefs.json"),
            "WEBDAV_CFG_FILE": str(self.state_root / "webdav.json"),
            "WEBDAV_LOGS_FILE": str(self.state_root / "webdav-logs.json"),
            "WEBDAV_COUNT_FILE": str(self.state_root / "webdav-count.json"),
            "PROXY_FILE": str(self.state_root / "proxy.json"),
            "DOWNLOAD_DIR": str(self.download_root),
            "AUTO_DELETE_SECONDS": 0,
            "ALLOWED_USERS": {42},
            "MediaDownloader": FakeDownloader,
            "MediaPublisher": FakePublisher,
        }
        self.patchers = [patch.object(bot, key, value) for key, value in replacements.items()]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = FakeClient()
        self.pipeline = bot.register_handlers(self.client, repository=self.repo, start_workers=False)

    async def test_queue_detail_and_confirmed_cache_delete(self) -> None:
        job_dir = self.download_root / "job-501"
        job_dir.mkdir()
        path = job_dir / "cached.mp4"
        path.write_bytes(b"cached")
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="failed",
            source_kind="url",
            legacy_seq=501,
            items=[{"local_path": str(path), "size_bytes": 6, "metadata": {"schema_version": 1}}],
            event_payload={"schema_version": 1},
        )
        callback = self.client.handlers["on_callback"]

        page_event = FakeCallbackEvent(self.client, b"q:p:failed:0")
        await callback(page_event)
        self.assertIn("筛选：失败", page_event.edits[-1]["text"])

        detail_event = FakeCallbackEvent(self.client, f"j:v:{job.id}".encode())
        await callback(detail_event)
        self.assertIn("本地缓存：存在", detail_event.edits[-1]["text"])

        stale_event = FakeCallbackEvent(self.client, f"j:d:{job.id}:{job.revision + 1}".encode())
        await callback(stale_event)
        self.assertIn("任务状态已变化，请刷新", stale_event.answers)
        self.assertTrue(path.exists())

        request = FakeCallbackEvent(self.client, f"j:d:{job.id}:{job.revision}".encode())
        await callback(request)
        confirm_buttons = request.edits[-1]["buttons"]
        yes = next(button.data for row in confirm_buttons for button in row if button.data.startswith(b"x:y:"))
        confirm = FakeCallbackEvent(self.client, yes)
        await callback(confirm)
        self.assertIn("缓存已删除", confirm.answers)
        self.assertFalse(path.exists())
        refreshed = await self.repo.job_detail(job.id, user_id=42)
        self.assertIsNone(refreshed["items"][0]["local_path"])

    async def test_confirmed_cancel_works_for_durable_only_job(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="queued",
            source_kind="url",
            legacy_seq=777,
            event_payload={"schema_version": 1},
        )
        callback = self.client.handlers["on_callback"]
        request = FakeCallbackEvent(self.client, f"j:c:{job.id}:{job.revision}".encode())
        await callback(request)
        yes = next(
            button.data
            for row in request.edits[-1]["buttons"]
            for button in row
            if button.data.startswith(b"x:y:")
        )
        confirm = FakeCallbackEvent(self.client, yes)
        await callback(confirm)
        self.assertIn("已取消任务", confirm.answers)
        refreshed = await self.repo.get_job(job.id)
        self.assertEqual(refreshed.state, "cancelled")

    async def test_batch_cancel_freezes_targets_and_executes_independently(self) -> None:
        first = await self.repo.accept_job(
            kind="url", user_id=42, state="queued", source_kind="url", legacy_seq=801,
            event_payload={"schema_version": 1},
        )
        second = await self.repo.accept_job(
            kind="url", user_id=42, state="queued", source_kind="url", legacy_seq=802,
            event_payload={"schema_version": 1},
        )
        callback = self.client.handlers["on_callback"]
        request = FakeCallbackEvent(self.client, b"q:bc")
        await callback(request)
        yes = next(
            button.data
            for row in request.edits[-1]["buttons"]
            for button in row
            if button.data.startswith(b"x:y:")
        )
        confirm = FakeCallbackEvent(self.client, yes)
        await callback(confirm)
        self.assertTrue(any("批量操作完成" in answer for answer in confirm.answers))
        self.assertEqual((await self.repo.get_job(first.id)).state, "cancelled")
        self.assertEqual((await self.repo.get_job(second.id)).state, "cancelled")

    async def test_confirmed_undo_deletes_each_peer_and_marks_refs(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="publishing",
            source_kind="url",
            legacy_seq=901,
            event_payload={"schema_version": 1},
        )
        published = await self.repo.record_published_messages(
            job.id,
            [(111, 10, "channel"), (222, 20, "discussion")],
            expected_revision=job.revision,
        )
        callback = self.client.handlers["on_callback"]
        request = FakeCallbackEvent(
            self.client,
            f"j:u:{job.id}:{published.job.revision}".encode(),
        )
        await callback(request)
        yes = next(
            button.data
            for row in request.edits[-1]["buttons"]
            for button in row
            if button.data.startswith(b"x:y:")
        )
        confirm = FakeCallbackEvent(self.client, yes)
        await callback(confirm)
        self.assertEqual(self.client.deleted_messages, [(111, 10), (222, 20)])
        refs = await self.repo.list_published_messages(job.id)
        self.assertTrue(all(ref.deleted_at is not None for ref in refs))


if __name__ == "__main__":
    unittest.main()
