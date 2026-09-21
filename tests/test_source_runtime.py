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


def _item(chat_id: int, message_id: int):
    return SimpleNamespace(source_chat_id=chat_id, source_message_id=message_id)


class FakeReader:
    def __init__(self, *, chats=(-1001, -1002), media=None, group=None) -> None:
        self._chats = list(chats)
        self._media = media or []
        self._group = group if group is not None else [1, 2, 3]
        self.listed: list[tuple[int, int, int, object]] = []
        self.captured_at: list[tuple[int, int]] = []
        self.groups: list[tuple[int, int]] = []
        self.ad_inputs: list[tuple[object, object]] = []
        self.submitted_inputs: list[object] = []
        self.done_inputs: list[object] = []

    def ordered_chats(self):
        return list(self._chats)

    def label_for(self, chat_id):
        return {-1001: "@first", -1002: "@second"}.get(int(chat_id), str(chat_id))

    async def list_recent_media(
        self,
        chat_id,
        *,
        limit=10,
        offset=0,
        since=None,
        learned=None,
        released=None,
        submitted=None,
        done=None,
    ):
        self.listed.append((int(chat_id), int(limit), int(offset), since))
        self.ad_inputs.append((learned, released))
        self.submitted_inputs.append(submitted)
        self.done_inputs.append(done)
        return (list(self._media), False)

    async def capture_at(self, chat_id, message_id):
        self.captured_at.append((int(chat_id), int(message_id)))
        return list(self._media)

    async def capture_many(self, chat_id, message_ids):
        self.captured_at.extend(
            (int(chat_id), int(message_id)) for message_id in message_ids
        )
        return list(self._media)

    async def group_message_ids(self, chat_id, message_id):
        self.groups.append((int(chat_id), int(message_id)))
        return list(self._group)


class FakeSubmittedRepository:
    """Minimal repository exposing the intake lookup used by the picker."""

    def __init__(self, ids: dict[int, set[int]] | None = None) -> None:
        self.ids = {int(chat): set(values) for chat, values in (ids or {}).items()}
        self.calls: list[int] = []
        self.flags: dict[str, str] = {}

    async def list_intake_source_ids(self, chat_id: int, **_kwargs) -> set[int]:
        self.calls.append(int(chat_id))
        return set(self.ids.get(int(chat_id), set()))

    async def set_runtime_flag(self, key: str, value: str) -> None:
        self.flags[str(key)] = str(value)


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

    async def test_list_media_forwards_the_submitted_ids(self) -> None:
        coordinator = self._coordinator()
        reader = FakeReader(media=["a"])
        coordinator._reader = reader
        coordinator._client = object()
        coordinator._repository = FakeSubmittedRepository({-1001: {11, 12}})

        await coordinator.list_media(0, page=0, page_size=10)
        self.assertEqual(reader.submitted_inputs, [frozenset({11, 12})])

    async def test_list_media_scan_cache_is_dropped_after_a_grab(self) -> None:
        coordinator = self._coordinator()
        reader = FakeReader(media=["a"])
        coordinator._reader = reader
        coordinator._client = object()
        coordinator._user_id = 7
        coordinator._repository = FakeSubmittedRepository({-1001: {11}})

        async def _hook(owner_id, media, label="", merge=False):
            return (len(list(media)), 0)

        coordinator._on_source_media = _hook
        await coordinator.list_media(0, page=0, page_size=10)
        self.assertEqual(len(reader.listed), 1)
        self.assertEqual(await coordinator.grab_message(0, 11), (1, "@first", 1, 0))
        await coordinator.list_media(0, page=0, page_size=10)
        self.assertEqual(len(reader.listed), 2)
        self.assertEqual(reader.submitted_inputs[-1], frozenset({11}))

    async def test_done_fingerprints_are_remembered_and_forgotten(self) -> None:
        coordinator = self._coordinator()
        reader = FakeReader(media=["a"])
        coordinator._reader = reader
        coordinator._client = object()
        coordinator._user_id = 7
        coordinator._repository = FakeSubmittedRepository({-1001: {11}})

        async def _hook(owner_id, media, label="", merge=False):
            return (len(list(media)), 0)

        coordinator._on_source_media = _hook
        self.assertEqual(await coordinator.grab_message(0, 11, fingerprint="photo|1|2x3|x"),
                         (1, "@first", 1, 0))
        await coordinator.list_media(0, page=0, page_size=10)
        self.assertEqual(reader.done_inputs[-1], frozenset({"photo|1|2x3|x"}))

        self.assertTrue(
            await coordinator.forget_done_fingerprint(0, "photo|1|2x3|x")
        )
        await coordinator.list_media(0, page=0, page_size=10)
        self.assertEqual(reader.done_inputs[-1], frozenset())
        self.assertFalse(
            await coordinator.forget_done_fingerprint(0, "photo|1|2x3|x")
        )

    async def test_grab_selection_remembers_the_row_fingerprints(self) -> None:
        coordinator = self._coordinator()
        coordinator._reader = FakeReader(media=[_item(-1001, 11)])
        coordinator._client = object()
        coordinator._user_id = 7
        coordinator._repository = FakeSubmittedRepository({-1001: {11}})

        async def _hook(owner_id, media, label="", merge=False):
            return (1, 0)

        coordinator._on_source_media = _hook
        await coordinator.grab_selection(
            [(0, 11)], fingerprints=["video|500|720x1280|caption"]
        )
        await coordinator.list_media(0, page=0, page_size=10)
        self.assertEqual(
            coordinator._reader.done_inputs[-1], frozenset({"video|500|720x1280|caption"})
        )

    async def test_list_media_without_a_reader_is_empty(self) -> None:
        coordinator = self._coordinator()
        self.assertEqual(await coordinator.list_media(0), ([], False, ""))

    async def test_list_media_reuses_a_recent_scan(self) -> None:
        coordinator = self._coordinator()
        reader = FakeReader(media=["a"])
        coordinator._reader = reader
        coordinator._client = object()

        first, _more, _label = await coordinator.list_media(0, page=0, page_size=400)
        second, _more, _label = await coordinator.list_media(0, page=0, page_size=400)
        self.assertEqual(len(reader.listed), 1)
        self.assertEqual([item for item in second], ["a"])
        self.assertEqual(first, second)

    async def test_list_media_refresh_bypasses_the_scan_cache(self) -> None:
        coordinator = self._coordinator()
        reader = FakeReader(media=["a"])
        coordinator._reader = reader
        coordinator._client = object()

        await coordinator.list_media(0, page=0, page_size=400)
        await coordinator.list_media(0, page=0, page_size=400, refresh=True)
        self.assertEqual(len(reader.listed), 2)

    async def test_list_media_scan_cache_is_dropped_after_a_release(self) -> None:
        coordinator = self._coordinator()
        reader = FakeReader(media=["a"])
        coordinator._reader = reader
        coordinator._client = object()

        await coordinator.list_media(0, page=0, page_size=400)
        self.assertTrue(await coordinator.release_fingerprint(0, "photo|1|2x3|x"))
        await coordinator.list_media(0, page=0, page_size=400)
        self.assertEqual(len(reader.listed), 2)

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

        async def _media(owner_id, media, label="", merge=False):
            captured.append((int(owner_id), list(media)))
            return (len(list(media)), 0)

        coordinator._on_source_media = _media
        count, label, accepted, skipped = await coordinator.grab_message(1, 30506)
        self.assertEqual((count, label, accepted, skipped), (1, "@second", 1, 0))
        self.assertEqual(coordinator._reader.captured_at, [(-1002, 30506)])
        self.assertEqual(captured, [(7, ["media"])])

    async def test_grab_message_reports_a_deleted_message(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        coordinator._reader = FakeReader(media=[])
        coordinator._client = object()
        count, label, accepted, skipped = await coordinator.grab_message(0, 404)
        self.assertEqual((count, label, accepted, skipped), (0, "@first", 0, 0))

    async def test_grab_selection_merges_rows_into_one_dispatch(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        captured: list[tuple[int, list, bool]] = []

        class _Reader(FakeReader):
            async def capture_many(self, chat_id, message_ids):
                self.groups.append((int(chat_id), tuple(int(mid) for mid in message_ids)))
                if int(chat_id) == -1001:
                    return [_item(-1001, 10), _item(-1001, 11)]
                return [_item(-1002, 20)]

        reader = _Reader()
        coordinator._reader = reader
        coordinator._client = object()

        async def _media(owner_id, media, label="", merge=False):
            captured.append((int(owner_id), list(media), bool(merge)))
            return (len(list(media)), 1)

        coordinator._on_source_media = _media
        count, label, failed, accepted, skipped = await coordinator.grab_selection(
            [(0, 10), (1, 20)]
        )
        self.assertEqual((count, label, failed, accepted, skipped), (3, "多个来源", 0, 3, 1))
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0][2])
        self.assertEqual([item.source_message_id for item in captured[0][1]], [10, 11, 20])

    async def test_grab_selection_preserves_interleaved_cross_source_order(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        captured: list[list] = []

        class _Reader(FakeReader):
            async def capture_many(self, chat_id, message_ids):
                message_id = int(message_ids[0])
                return [_item(int(chat_id), message_id)]

        coordinator._reader = _Reader()
        coordinator._client = object()

        async def _media(_owner_id, media, _label="", merge=False):
            self.assertTrue(merge)
            captured.append(list(media))
            return (len(media), 0)

        coordinator._on_source_media = _media
        await coordinator.grab_selection([(0, 10), (1, 20), (0, 11)])
        self.assertEqual(
            [(item.source_chat_id, item.source_message_id) for item in captured[0]],
            [(-1001, 10), (-1002, 20), (-1001, 11)],
        )

    async def test_partial_merge_does_not_hide_unaccepted_fingerprints(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        coordinator._reader = FakeReader(media=[_item(-1001, 11)])
        coordinator._client = object()
        coordinator._repository = FakeSubmittedRepository()

        async def _media(_owner_id, _media, _label="", merge=False):
            self.assertTrue(merge)
            return (0, 1)

        coordinator._on_source_media = _media
        await coordinator.grab_selection([(0, 11)], fingerprints=["video|500|720x1280|caption"])
        await coordinator.list_media(0, page=0, page_size=10)
        self.assertEqual(coordinator._reader.done_inputs[-1], frozenset())

    async def test_grab_selection_skips_a_row_that_cannot_be_read(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        captured: list[tuple[int, list, bool]] = []

        class _Reader(FakeReader):
            async def capture_many(self, chat_id, message_ids):
                if int(chat_id) == -1002:
                    raise RuntimeError("boom")
                return [_item(-1001, 10)]

        coordinator._reader = _Reader()
        coordinator._client = object()

        async def _media(owner_id, media, label="", merge=False):
            captured.append((int(owner_id), list(media), bool(merge)))
            return (len(list(media)), 0)

        coordinator._on_source_media = _media
        count, label, failed, _accepted, _skipped = await coordinator.grab_selection(
            [(0, 10), (1, 20)]
        )
        self.assertEqual((count, label, failed), (1, "@first", 1))
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0][2])
        self.assertEqual([item.source_message_id for item in captured[0][1]], [10])

    async def test_grab_selection_without_media_reports_failure(self) -> None:
        coordinator = self._coordinator()
        coordinator._user_id = 7
        coordinator._reader = FakeReader(media=[])
        coordinator._client = object()
        self.assertEqual(await coordinator.grab_selection([]), (0, "", 0, 0, 0))

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
