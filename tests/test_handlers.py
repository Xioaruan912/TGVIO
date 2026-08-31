import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src import bot
from src.webdav import WebDavProbeResult, WebDavWriteProbeResult
from src.handlers.callbacks import build_callback_router
from src.services import BackupManager, InteractionSessions, JobQueue, ProxyManager
from tests.fakes import (
    FakeCallbackEvent,
    FakeClient,
    FakeDownloader,
    FakeNewMessageEvent,
    FakePublisher,
)


class HandlerBoundaryTests(unittest.IsolatedAsyncioTestCase):
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
            "AUTO_DELETE_SECONDS": 0,
            "ALLOWED_USERS": {42},
            "MediaDownloader": FakeDownloader,
            "MediaPublisher": FakePublisher,
        }
        self.patchers = [
            patch.object(bot, key, value) for key, value in replacements.items()
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = FakeClient()
        with patch.object(bot._Pipeline, "start", autospec=True) as start:
            self.pipeline = bot.register_handlers(self.client)
            start.assert_called_once_with(self.pipeline)
        self.pipeline._counter = 700

    def test_registration_installs_extracted_handlers_and_facades(self) -> None:
        self.assertEqual(
            set(self.client.handlers),
            {
                "on_start",
                "on_about",
                "on_stats",
                "on_health",
                "on_diag",
                "on_mode",
                "on_webdav",
                "on_webdavlogs",
                "on_proxy",
                "on_begin",
                "on_end",
                "on_queue",
                "on_callback",
                "on_private_message",
            },
        )
        self.assertIsInstance(self.pipeline.job_queue, JobQueue)
        self.assertIsInstance(self.pipeline.backup_manager, BackupManager)
        self.assertIsInstance(self.pipeline.proxy_manager, ProxyManager)
        self.assertIsInstance(self.pipeline.interactions, InteractionSessions)
        self.assertFalse(hasattr(self.pipeline, "webdav_waiting"))
        self.assertFalse(hasattr(self.pipeline, "proxy_waiting"))

    async def test_stale_and_unauthorized_callbacks_are_answered(self) -> None:
        callback = self.client.handlers["on_callback"]
        stale = FakeCallbackEvent(self.client, b"removed:action")
        await callback(stale)
        self.assertEqual(stale.answers[-1], "操作已过期，请刷新")

        unauthorized = FakeCallbackEvent(
            self.client,
            b"q_pause",
            sender_id=999,
        )
        await callback(unauthorized)
        self.assertEqual(unauthorized.answers[-1], "无权限")
        self.assertFalse(self.pipeline._paused)

    def test_callback_router_resolves_exact_and_prefix_actions(self) -> None:
        router = build_callback_router()
        self.assertIsNotNone(router.resolve("h:r"))
        self.assertIsNotNone(router.resolve("q_pause"))
        self.assertIsNotNone(router.resolve("confirm:10:0"))
        self.assertIsNotNone(router.resolve("wd_cfg:on"))
        self.assertIsNotNone(router.resolve("proxy:add"))
        self.assertIsNotNone(router.resolve("session_end:42"))
        self.assertIsNone(router.resolve("unknown:action"))

    async def test_start_is_stable_home_console_and_home_refresh_edits_message(self) -> None:
        start = self.client.handlers["on_start"]
        event = FakeNewMessageEvent(self.client, "/start")
        await start(event)
        self.assertEqual(len(event.responses), 1)
        self.assertIn("🤖 Telegram 媒体中转站", event.responses[0].text)
        sent = self.client.sent_messages[-1]
        callbacks = [button.data for row in sent["buttons"] for button in row]
        self.assertIn(b"h:r", callbacks)
        self.assertIn(b"h:q", callbacks)

        callback = self.client.handlers["on_callback"]
        refresh = FakeCallbackEvent(self.client, b"h:r")
        await callback(refresh)
        self.assertIn("🤖 Telegram 媒体中转站", refresh.edits[-1]["text"])

        end = FakeCallbackEvent(self.client, b"h:end")
        await callback(end)
        self.assertIn("当前没有进行中的合集", end.answers)
        self.assertIn("📥 当前合集：未开始", end.edits[-1]["text"])

    async def test_webdav_input_uses_explicit_interaction_session(self) -> None:
        callback = self.client.handlers["on_callback"]
        private = self.client.handlers["on_private_message"]

        prompt = FakeCallbackEvent(self.client, b"wd_cfg:path")
        await callback(prompt)
        session = self.pipeline.interactions.get(42)
        self.assertIsNotNone(session)
        self.assertEqual((session.kind, session.field), ("webdav", "path"))

        event = FakeNewMessageEvent(self.client, "archive")
        await private(event)

        self.assertEqual(self.pipeline.webdav_cfg["path"], "/archive")
        self.assertIsNone(self.pipeline.interactions.get(42))
        self.assertIn("已更新 WebDAV path", event.responses[0].text)

    async def test_required_backup_policy_requires_explicit_confirmation(self) -> None:
        callback = self.client.handlers["on_callback"]
        prompt = FakeCallbackEvent(self.client, b"wd_cfg:policy")
        await callback(prompt)
        callbacks = [button.data for row in prompt.edits[-1]["buttons"] for button in row]
        confirm = next(data for data in callbacks if data.startswith(b"wd_bp:y:"))
        self.assertEqual(self.pipeline.webdav_cfg.get("backup_policy"), "best_effort")

        confirm_event = FakeCallbackEvent(self.client, confirm)
        await callback(confirm_event)
        self.assertEqual(self.pipeline.webdav_cfg.get("backup_policy"), "required")

        disable = FakeCallbackEvent(self.client, b"wd_cfg:policy")
        await callback(disable)
        self.assertEqual(self.pipeline.webdav_cfg.get("backup_policy"), "best_effort")

    async def test_webdav_remote_probe_runs_only_on_explicit_test_callback(self) -> None:
        callback = self.client.handlers["on_callback"]
        self.pipeline.webdav_cfg.update(
            url="https://dav.example.invalid",
            path="/backup",
            user="user",
            **{"pass": "secret"},
        )
        probe = AsyncMock(
            return_value=WebDavProbeResult(
                True,
                207,
                True,
                quota_available_bytes=1024,
                message="读取成功",
            )
        )
        self.pipeline.backup_manager.test_connection = probe

        open_page = FakeCallbackEvent(self.client, b"h:w")
        await callback(open_page)
        probe.assert_not_awaited()

        explicit = FakeCallbackEvent(self.client, b"wd_cfg:test")
        await callback(explicit)
        probe.assert_awaited_once_with()
        self.assertIn("✅ 路径可读取", explicit.edits[-1]["text"])
        self.assertNotIn("secret", explicit.edits[-1]["text"])

    async def test_webdav_write_probe_requires_single_use_confirmation(self) -> None:
        callback = self.client.handlers["on_callback"]
        self.pipeline.webdav_cfg.update(
            url="https://dav.example.invalid",
            path="/backup",
            user="user",
            **{"pass": "secret"},
        )
        writer = AsyncMock(
            return_value=WebDavWriteProbeResult(True, True, True, True, "全部成功")
        )
        self.pipeline.backup_manager.test_write = writer

        prompt = FakeCallbackEvent(self.client, b"wd_cfg:wtest")
        await callback(prompt)
        writer.assert_not_awaited()
        confirm_data = prompt.edits[-1]["buttons"][0][0].data

        confirm = FakeCallbackEvent(self.client, confirm_data)
        await callback(confirm)
        writer.assert_awaited_once_with()
        self.assertIn("测试文件清理：成功", confirm.edits[-1]["text"])

        duplicate = FakeCallbackEvent(self.client, confirm_data)
        await callback(duplicate)
        self.assertIn("操作已过期，请刷新", duplicate.answers)
        self.assertEqual(writer.await_count, 1)

    async def test_private_url_intake_routes_through_job_queue_facade(self) -> None:
        private = self.client.handlers["on_private_message"]
        event = FakeNewMessageEvent(self.client, "https://example.invalid/video")

        await private(event)

        job = self.pipeline.input_q.get_nowait()
        self.pipeline.input_q.task_done()
        self.assertEqual(job.seq, 700)
        self.assertEqual(job.kind, "url")
        self.assertEqual(job.url, "https://example.invalid/video")
        self.assertIn("已加入队列", event.replies[0].text)


class InteractionSessionTests(unittest.TestCase):
    def test_revision_prevents_old_input_from_clearing_new_session(self) -> None:
        sessions = InteractionSessions()
        first = sessions.start(42, "webdav", "path")
        second = sessions.start(42, "proxy", "add")

        self.assertGreater(second.revision, first.revision)
        self.assertFalse(sessions.finish(42, first.revision))
        self.assertEqual(sessions.get(42), second)
        self.assertTrue(sessions.finish(42, second.revision))
        self.assertIsNone(sessions.get(42))


if __name__ == "__main__":
    unittest.main()
