from __future__ import annotations

import json
from datetime import datetime, timezone
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
        "source_download_workers": 4,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeReader:
    def __init__(self, *, chats=(-1001, -1002), media=None, group=None) -> None:
        self._chats = list(chats)
        self._media = media or []
        self._group = group if group is not None else [1, 2, 3]
        self.listed: list[tuple[int, int, int, object]] = []
        self.captured_at: list[tuple[int, int]] = []
        self.groups: list[tuple[int, int]] = []

    def ordered_chats(self):
        return list(self._chats)

    def label_for(self, chat_id):
        return {-1001: "@first", -1002: "@second"}.get(int(chat_id), str(chat_id))

    async def list_recent_media(self, chat_id, *, limit=10, offset=0, since=None):
        self.listed.append((int(chat_id), int(limit), int(offset), since))
        return (list(self._media), False)

    async def capture_at(self, chat_id, message_id):
        self.captured_at.append((int(chat_id), int(message_id)))
        return list(self._media)

    async def group_message_ids(self, chat_id, message_id):
        self.groups.append((int(chat_id), int(message_id)))
        return list(self._group)


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
        self.assertEqual(coordinator.effective_chats(), ("@env",))
        self.assertEqual(coordinator.status_line(), "未登录")
        self.assertEqual(coordinator.source_count(), 1)
        self.assertEqual(coordinator.source_label(0), "@env")
        self.assertFalse(coordinator.active)

    async def test_runtime_flags_override_settings(self) -> None:
        coordinator = self._coordinator()
        await self.flags.set(self.repo, "source_chats", json.dumps(["@a", "-1001"]))
        self.assertEqual(coordinator.effective_chats(), ("@a", "-1001"))

    async def test_invalid_stored_whitelist_falls_back_to_settings(self) -> None:
        coordinator = self._coordinator()
        await self.flags.set(self.repo, "source_chats", "{not json")
        self.assertEqual(coordinator.effective_chats(), ("@env",))

    async def test_list_media_pages_through_the_selected_source(self) -> None:
        coordinator = self._coordinator()
        reader = FakeReader(media=["a", "b"])
        coordinator._reader = reader
        coordinator._client = object()

        media, has_more, label = await coordinator.list_media(1, page=2, page_size=10)
        self.assertEqual(label, "@second")
        self.assertEqual(reader.listed, [(-1002, 10, 20, None)])
        self.assertEqual(media, ["a", "b"])
        self.assertFalse(has_more)

    async def test_list_media_forwards_the_window_start(self) -> None:
        coordinator = self._coordinator()
        reader = FakeReader()
        coordinator._reader = reader
        coordinator._client = object()
        since = datetime(2026, 9, 19, tzinfo=timezone.utc)
        await coordinator.list_media(0, page=0, page_size=10, since=since)
        self.assertEqual(reader.listed, [(-1001, 10, 0, since)])

    async def test_list_media_without_a_reader_is_empty(self) -> None:
        coordinator = self._coordinator()
        self.assertEqual(await coordinator.list_media(0), ([], False, ""))

    async def test_group_message_ids_uses_the_selected_source(self) -> None:
        coordinator = self._coordinator()
        reader = FakeReader(group=[7, 8, 9])
        coordinator._reader = reader
        coordinator._client = object()
        ids = await coordinator.group_message_ids(1, 30506)
        self.assertEqual(ids, [7, 8, 9])
        self.assertEqual(reader.groups, [(-1002, 30506)])

    async def test_group_message_ids_without_a_reader_is_empty(self) -> None:
        coordinator = self._coordinator()
        self.assertEqual(await coordinator.group_message_ids(0, 5), [])

    async def test_grab_message_dispatches_the_selected_message(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        captured: list[tuple[int, list]] = []
        coordinator._reader = FakeReader(media=["media"])
        coordinator._client = object()

        async def _media(owner_id, media, label=""):
            captured.append((int(owner_id), list(media)))

        coordinator._on_source_media = _media
        count, label = await coordinator.grab_message(1, 30506)
        self.assertEqual((count, label), (1, "@second"))
        self.assertEqual(coordinator._reader.captured_at, [(-1002, 30506)])
        self.assertEqual(captured, [(7, ["media"])])

    async def test_grab_message_reports_a_deleted_message(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        coordinator._reader = FakeReader(media=[])
        coordinator._client = object()
        count, label = await coordinator.grab_message(0, 404)
        self.assertEqual((count, label), (0, "@first"))

    async def test_grab_latest_is_gone(self) -> None:
        coordinator = self._coordinator()
        self.assertFalse(hasattr(coordinator, "grab_latest"))

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
