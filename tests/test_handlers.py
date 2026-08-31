import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from telethon.tl.types import MessageMediaPhoto

from src import bot
from src.webdav import WebDavProbeResult, WebDavWriteProbeResult
from src.handlers.callbacks import build_callback_router
from src.repository import SQLiteRepository
from src.services import BackupManager, InteractionSessions, JobQueue, ProxyManager
from tests.fakes import (
    FakeCallbackEvent,
    FakeClient,
    FakeDownloader,
    FakeMessage,
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
                "on_profiles",
                "on_sources",
                "on_webdav",
                "on_webdavlogs",
                "on_proxy",
                "on_begin",
                "on_end",
                "on_queue",
                "on_callback",
                "on_private_message",
                "on_source_message",
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
        self.assertIn("Telegram 媒体中转站", event.responses[0].text)
        self.assertIn("/queue — 查看运行、等待、失败任务", event.responses[0].text)
        self.assertIn("/about — 查看完整命令说明", event.responses[0].text)
        sent = self.client.sent_messages[-1]
        callbacks = [button.data for row in sent["buttons"] for button in row]
        self.assertEqual(callbacks, [])

        callback = self.client.handlers["on_callback"]
        refresh = FakeCallbackEvent(self.client, b"h:r")
        await callback(refresh)
        self.assertIn("Telegram 媒体中转站", refresh.edits[-1]["text"])

        end = FakeCallbackEvent(self.client, b"h:end")
        await callback(end)
        self.assertIn("当前没有进行中的合集", end.answers)
        self.assertIn("合集：未开始", end.edits[-1]["text"])

    async def test_destination_profile_test_requires_confirmation_and_cleans_message(self) -> None:
        repo = SQLiteRepository(
            os.path.join(self.tempdir.name, "profiles.sqlite3"),
            download_root=os.path.join(self.tempdir.name, "downloads"),
        )
        await repo.open()
        await repo.migrate()
        self.addAsyncCleanup(repo.close)
        default = await repo.ensure_env_destination_profile(
            destination_peer="@default",
            channel_at="@default",
        )
        client = FakeClient()
        with patch.object(bot._Pipeline, "start", autospec=True):
            pipeline = bot.register_handlers(
                client,
                repository=repo,
                default_destination_profile=default,
            )
        callback = client.handlers["on_callback"]
        profile = await pipeline.destination_profiles.create_profile(
            name="测试目的地",
            destination_peer="@archive",
        )

        before = len(client.sent_messages)
        prompt = FakeCallbackEvent(client, f"dp:t:{profile.id}".encode())
        await callback(prompt)
        self.assertEqual(len(client.sent_messages), before)
        confirm_buttons = [
            button.data
            for row in prompt.edits[-1]["buttons"]
            for button in row
            if button.data.startswith(b"dp:tc:")
        ]
        self.assertEqual(len(confirm_buttons), 1)

        sent = type("_Sent", (), {"id": 9001, "delete": AsyncMock()})()
        client.get_input_entity = AsyncMock(return_value="archive-input")
        client.send_message = AsyncMock(return_value=sent)
        confirmed = FakeCallbackEvent(client, confirm_buttons[0])
        await callback(confirmed)

        client.get_input_entity.assert_awaited_once_with("@archive")
        client.send_message.assert_awaited_once()
        sent.delete.assert_awaited_once()

    async def test_enabled_source_profile_enqueues_each_source_message_once(self) -> None:
        repo = SQLiteRepository(
            os.path.join(self.tempdir.name, "sources.sqlite3"),
            download_root=os.path.join(self.tempdir.name, "downloads"),
        )
        await repo.open()
        await repo.migrate()
        self.addAsyncCleanup(repo.close)
        default = await repo.ensure_env_destination_profile(
            destination_peer="@default",
            channel_at="@default",
        )
        client = FakeClient()
        with patch.object(bot._Pipeline, "start", autospec=True):
            pipeline = bot.register_handlers(
                client,
                repository=repo,
                default_destination_profile=default,
            )
        source = await pipeline.source_profiles.create_verified(
            name="来源",
            source_peer="@source",
            source_peer_id=-10012345,
            destination_profile_id=default.id,
            owner_user_id=42,
        )
        self.assertEqual(await pipeline.source_profiles.set_enabled(source.id, True), "ok")

        message = FakeMessage(
            77,
            media=MessageMediaPhoto(photo=None),
        )
        event = FakeNewMessageEvent(
            client,
            "",
            sender_id=999,
            chat_id=-10012345,
            message=message,
        )
        source_handler = client.handlers["on_source_message"]
        await source_handler(event)
        first_active = set(pipeline.active_seqs)
        self.assertEqual(len(first_active), 1)
        await pipeline.job_queue.drain_shadow()

        await source_handler(event)
        self.assertEqual(set(pipeline.active_seqs), first_active)
        conn = repo._require_conn()
        count = await (await conn.execute("SELECT COUNT(*) AS n FROM source_events")).fetchone()
        self.assertEqual(count["n"], 1)
        row = await (await conn.execute("SELECT state,job_legacy_seq FROM source_events")).fetchone()
        self.assertEqual(row["state"], "enqueued")
        self.assertIsNotNone(row["job_legacy_seq"])
        jobs = await (await conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE legacy_seq=?", (row["job_legacy_seq"],))).fetchone()
        self.assertEqual(jobs["n"], 1)

    async def test_source_grouped_media_becomes_one_album_job(self) -> None:
        repo = SQLiteRepository(
            os.path.join(self.tempdir.name, "source-album.sqlite3"),
            download_root=os.path.join(self.tempdir.name, "downloads"),
        )
        await repo.open()
        await repo.migrate()
        self.addAsyncCleanup(repo.close)
        default = await repo.ensure_env_destination_profile(
            destination_peer="@default",
            channel_at="@default",
        )
        client = FakeClient()
        with patch.object(bot._Pipeline, "start", autospec=True):
            pipeline = bot.register_handlers(
                client,
                repository=repo,
                default_destination_profile=default,
            )
        source = await pipeline.source_profiles.create_verified(
            name="相册来源",
            source_peer="@album-source",
            source_peer_id=-10022345,
            destination_profile_id=default.id,
            owner_user_id=42,
        )
        await pipeline.source_profiles.update(source.id, album_gather_seconds=0.5)
        await pipeline.source_profiles.set_enabled(source.id, True)

        source_handler = client.handlers["on_source_message"]
        for message_id in (10, 11):
            event = FakeNewMessageEvent(
                client,
                "",
                sender_id=999,
                chat_id=-10022345,
                message=FakeMessage(
                    message_id,
                    media=MessageMediaPhoto(photo=None),
                    grouped_id=555,
                ),
            )
            await source_handler(event)
        await asyncio.sleep(0.65)
        await pipeline.job_queue.drain_shadow()

        conn = repo._require_conn()
        rows = await (await conn.execute("SELECT state,job_legacy_seq FROM source_events ORDER BY id")).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["state"] == "enqueued" for row in rows))
        self.assertEqual(len({row["job_legacy_seq"] for row in rows}), 1)
        seq = int(rows[0]["job_legacy_seq"])
        self.assertEqual(pipeline._runtime_jobs[seq].kind, "album")
        self.assertEqual([item.id for item in pipeline._runtime_jobs[seq].album], [10, 11])

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
