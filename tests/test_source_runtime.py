from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tgvio.adapters.telegram.source_runtime import SourceCoordinator, SourceLoginError
from tgvio.application.runtime_flags import RuntimeFlags


class FakeRepository:
    def __init__(self) -> None:
        self.flags: dict[str, str] = {}

    async def set_runtime_flag(self, key: str, value: str) -> None:
        self.flags[str(key)] = str(value)


def _settings(**overrides):
    values = {
        "api_id": 123,
        "api_hash": "hash",
        "source_session": None,
        "source_chats": ("@env",),
        "source_trigger": "#env",
        "source_delete_trigger": True,
        "source_latest": True,
        "source_download_workers": 4,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SourceCoordinatorConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "source_user"
        self.repo = FakeRepository()
        self.flags = RuntimeFlags()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _coordinator(self, **overrides) -> SourceCoordinator:
        return SourceCoordinator(_settings(**overrides), self.repo, self.flags, self.path)

    async def test_defaults_come_from_settings(self) -> None:
        coordinator = self._coordinator()
        self.assertEqual(coordinator.effective_trigger(), "#env")
        self.assertEqual(coordinator.effective_chats(), ("@env",))
        self.assertTrue(coordinator.effective_delete_trigger())
        self.assertTrue(coordinator.effective_latest())
        self.assertEqual(coordinator.status_line(), "未登录")

    async def test_latest_toggle_persists(self) -> None:
        coordinator = self._coordinator()
        self.assertFalse(await coordinator.toggle_latest())
        self.assertFalse(coordinator.effective_latest())
        self.assertEqual(self.repo.flags["source_latest"], "false")
        self.assertTrue(await coordinator.toggle_latest())
        self.assertTrue(coordinator.effective_latest())

    async def test_check_now_processes_pending_triggers(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        processed: list[tuple[int, int]] = []

        class _Reader:
            async def find_recent_triggers(self, **_kwargs):
                return [(5, 100, None), (5, 99, 42)]

        async def _handle(chat_id, message_id, reply_to):
            processed.append((int(chat_id), int(message_id)))

        coordinator._reader = _Reader()
        coordinator.handle_trigger = _handle
        coordinator._trigger_seen = {5: 0}
        found, count = await coordinator.check_now()
        self.assertEqual(found, 2)
        self.assertEqual(count, 2)
        self.assertEqual(processed, [(5, 100), (5, 99)])

    async def test_check_now_ignores_already_seen_triggers(self) -> None:
        coordinator = self._coordinator()

        class _Reader:
            async def find_recent_triggers(self, **_kwargs):
                return [(5, 10, None)]

        coordinator._reader = _Reader()
        coordinator._trigger_seen = {5: 10}
        found, count = await coordinator.check_now()
        self.assertEqual(found, 1)
        self.assertEqual(count, 0)

    async def test_handle_trigger_dedupes_and_dispatches_media(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        captured: list[tuple[int, list]] = []
        notices: list[str] = []
        deleted: list[tuple[int, int]] = []

        class _Reader:
            def __init__(self) -> None:
                self.replies: list[tuple[int, int]] = []

            async def capture_at(self, chat_id, reply_to_msg_id):
                self.replies.append((chat_id, reply_to_msg_id))
                return ["media"]

            async def capture_latest(self, chat_id, **_kwargs):
                return ["latest"]

        class _Client:
            async def delete_messages(self, chat_id, message_ids, **_kwargs):
                deleted.append((int(chat_id), int(message_ids[0])))

        async def _media(owner_id, media):
            captured.append((int(owner_id), list(media)))

        async def _notice(text):
            notices.append(str(text))

        reader = _Reader()
        coordinator._reader = reader
        coordinator._client = _Client()
        coordinator._trigger_seen = {5: 10}
        coordinator.set_hooks(on_trigger_media=_media, on_notice=_notice)

        await coordinator.handle_trigger(5, 11, 99)
        self.assertEqual(reader.replies, [(5, 99)])
        self.assertEqual(captured, [(7, ["media"])])
        self.assertEqual(deleted, [(5, 11)])
        # A duplicate delivery of the same trigger message is ignored.
        await coordinator.handle_trigger(5, 11, 99)
        self.assertEqual(len(reader.replies), 1)

    async def test_handle_trigger_without_reply_falls_back_to_latest(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        captured: list[tuple[int, list]] = []

        class _Reader:
            async def capture_at(self, chat_id, reply_to_msg_id):
                return []

            async def capture_latest(self, chat_id, **_kwargs):
                return ["latest"]

        coordinator._reader = _Reader()

        async def _record(owner_id, media):
            captured.append((int(owner_id), list(media)))

        coordinator._on_trigger_media = _record
        await coordinator.handle_trigger(5, 1, None)
        self.assertEqual(captured, [(7, ["latest"])])

    async def test_handle_trigger_without_media_notices_the_owner(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        notices: list[str] = []

        class _Reader:
            async def capture_at(self, chat_id, reply_to_msg_id):
                return []

            async def capture_latest(self, chat_id, **_kwargs):
                return []

        coordinator._reader = _Reader()

        async def _notice(text):
            notices.append(str(text))

        coordinator._on_notice = _notice
        await coordinator.handle_trigger(5, 1, 42)
        self.assertTrue(notices)
        self.assertIn("未能读取", notices[0])

    async def test_runtime_flags_override_settings(self) -> None:
        coordinator = self._coordinator()
        await self.flags.set(self.repo, "source_trigger", "#custom")
        await self.flags.set(self.repo, "source_delete_trigger", "false")
        await self.flags.set(self.repo, "source_chats", json.dumps(["@a", "-1001"]))
        self.assertEqual(coordinator.effective_trigger(), "#custom")
        self.assertFalse(coordinator.effective_delete_trigger())
        self.assertEqual(coordinator.effective_chats(), ("@a", "-1001"))

    async def test_invalid_stored_whitelist_falls_back_to_settings(self) -> None:
        coordinator = self._coordinator()
        await self.flags.set(self.repo, "source_chats", "{not json")
        self.assertEqual(coordinator.effective_chats(), ("@env",))

    async def test_add_chat_requires_a_ready_client(self) -> None:
        coordinator = self._coordinator()
        with self.assertRaises(SourceLoginError):
            await coordinator.add_chat("@x")

    async def test_remove_chat_updates_persisted_whitelist(self) -> None:
        coordinator = self._coordinator()
        await self.flags.set(self.repo, "source_chats", json.dumps(["@a", "@b"]))
        message = await coordinator.remove_chat(0)
        self.assertIn("@a", message)
        self.assertEqual(coordinator.whitelist(), ["@b"])
        self.assertEqual(json.loads(self.repo.flags["source_chats"]), ["@b"])


if __name__ == "__main__":
    unittest.main()
