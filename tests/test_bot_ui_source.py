from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from tgvio.adapters.telegram.bot_ui_format import BotUIFormatMixin
from tgvio.adapters.telegram.bot_ui_source import BotUISourceMixin
from tgvio.adapters.telegram.user_source import SourceMediaSummary
from tgvio.domain.job import MediaKind


def _summary(
    message_id: int,
    *,
    kinds: tuple[MediaKind, ...] = (MediaKind.VIDEO,),
    item_count: int = 1,
    size: int = 1024,
    grouped_id: int | None = None,
    duration: float | None = 95.0,
) -> SourceMediaSummary:
    return SourceMediaSummary(
        message_id=message_id,
        kinds=kinds,
        item_count=item_count,
        size_bytes=size,
        duration_seconds=duration,
        date=datetime(2026, 9, 19, 3, 30, tzinfo=timezone.utc),
        grouped_id=grouped_id,
    )


class FakeCoordinator:
    def __init__(self, *, pages: dict[int, list] | None = None, count: int = 2) -> None:
        self.pages = {0: [_summary(30506)]} if pages is None else pages
        self.count = count
        self.calls: list[tuple[int, int]] = []
        self.listed: list[tuple[int, int, int]] = []

    active = True

    def status_line(self) -> str:
        return "已登录 · user_id `7` · 白名单 `2/2` 生效"

    def whitelist(self) -> list[str]:
        return ["@xiaodeFile_bot", "@chunziyuan_bot"]

    def source_label(self, index: int = 0) -> str:
        return "@xiaodeFile_bot"

    def set_awaiting(self, phase) -> None:
        self.phase = phase

    async def list_media(self, source_index: int = 0, *, page: int = 0, page_size: int = 10):
        self.listed.append((int(source_index), int(page), int(page_size)))
        summaries = self.pages.get(page, [])
        has_more = (page + 1) in self.pages
        return (list(summaries), has_more, "@xiaodeFile_bot")

    async def grab_message(self, source_index: int, message_id: int):
        self.calls.append((int(source_index), int(message_id)))
        return (self.count, "@xiaodeFile_bot")

    async def logout(self):
        return "已退出登录并删除 session。"

    async def remove_chat(self, index: int) -> str:
        return f"已移除来源 `#{index}`。"


class _Client:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((int(chat_id), str(text)))


class _Event:
    def __init__(self, chat_id: int = 7) -> None:
        self.chat_id = chat_id
        self.edits: list[tuple[str, list]] = []
        self.answers: list[str | None] = []


class _UI(BotUIFormatMixin, BotUISourceMixin):
    def __init__(self, coordinator: FakeCoordinator) -> None:
        self._source = coordinator
        self._client = _Client()

    async def _edit_page(self, event, text, buttons):
        event.edits.append((text, buttons))

    async def _safe_answer(self, event, text=None, **kwargs):
        event.answers.append(text)


def _callbacks(rows) -> list[bytes]:
    return [
        button.data
        for row in rows
        for button in row
        if getattr(button, "data", None)
    ]


class SourcePickPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_pick_page_lists_ten_items_without_captions(self) -> None:
        summaries = [
            _summary(30506 - index, kinds=(MediaKind.VIDEO,)) for index in range(10)
        ]
        coordinator = FakeCoordinator(pages={0: summaries, 1: [_summary(30000)]})
        ui = _UI(coordinator)
        text, rows = await ui._pick_page(7, 0, 0)

        self.assertIn("@xiaodeFile_bot", text)
        self.assertIn("🎬 视频", text)
        self.assertIn("10)", text)
        self.assertIn("点 📥 抓取该条", text)
        self.assertNotIn(".bin", text)
        self.assertNotIn(".jpg", text)

        encoded = _callbacks(rows)
        self.assertIn(b"ui:sg:0:30506", encoded)
        self.assertIn(b"ui:sp:0:1", encoded)
        self.assertTrue(all(len(data) <= 64 for data in encoded))

    async def test_pick_page_labels_albums_and_hides_page_nav_on_first_page(self) -> None:
        summaries = [
            _summary(
                100,
                kinds=(MediaKind.PHOTO, MediaKind.VIDEO),
                item_count=3,
                size=3 * 1024,
                grouped_id=99,
            )
        ]
        ui = _UI(FakeCoordinator(pages={0: summaries}))
        text, rows = await ui._pick_page(7, 0, 0)
        self.assertIn("🧩 相册 3 项", text)
        self.assertIn("🖼 图片", text)
        self.assertIn("🎬 视频", text)
        encoded = _callbacks(rows)
        self.assertNotIn(b"ui:sp:0:1", encoded)
        self.assertIn(b"ui:pick:0:0", encoded)

    async def test_pick_page_offers_previous_page(self) -> None:
        ui = _UI(FakeCoordinator(pages={1: [_summary(100)]}))
        text, rows = await ui._pick_page(7, 0, 1)
        self.assertIn("第 2 页", text)
        encoded = _callbacks(rows)
        self.assertIn(b"ui:sp:0:0", encoded)
        self.assertNotIn(b"ui:sp:0:2", encoded)

    async def test_pick_page_without_source_media(self) -> None:
        ui = _UI(FakeCoordinator(pages={}))
        text, rows = await ui._pick_page(7, 0, 0)
        self.assertIn("没有媒体", text)
        self.assertTrue(all(len(data) <= 64 for data in _callbacks(rows)))


class SourceCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_pick_callback_edits_the_page(self) -> None:
        coordinator = FakeCoordinator(pages={2: [_summary(500)]})
        ui = _UI(coordinator)
        event = _Event()
        handled = await ui._handle_source_callback(event, 7, "ui:pick:1:2")
        self.assertTrue(handled)
        self.assertEqual(coordinator.listed, [(1, 2, 10)])
        self.assertTrue(event.edits)

    async def test_grab_callback_dispatches_and_refreshes(self) -> None:
        coordinator = FakeCoordinator(pages={0: [_summary(30506)]})
        ui = _UI(coordinator)
        event = _Event()
        handled = await ui._handle_source_callback(event, 7, "ui:sg:0:30506")
        self.assertTrue(handled)
        self.assertEqual(coordinator.calls, [(0, 30506)])
        self.assertTrue(any("已抓取 2 个媒体" in text for _chat, text in ui._client.sent))
        self.assertTrue(event.edits)

    async def test_grab_callback_reports_a_deleted_message(self) -> None:
        ui = _UI(FakeCoordinator(count=0))
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:sg:0:404")
        self.assertTrue(any("不可读取" in text for _chat, text in ui._client.sent))

    async def test_page_callback_rejects_bad_payload(self) -> None:
        ui = _UI(FakeCoordinator())
        event = _Event()
        handled = await ui._handle_source_callback(event, 7, "ui:sp:x")
        self.assertTrue(handled)
        self.assertEqual(event.answers, ["操作已过期"])
        self.assertEqual(event.edits, [])

    async def test_source_page_and_unknown_actions(self) -> None:
        ui = _UI(FakeCoordinator())
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:source"))
        text, rows = event.edits[0]
        self.assertIn("来源账号", text)
        self.assertIn("/pick", text)
        self.assertIn(b"ui:pick:0:0", _callbacks(rows))

        self.assertFalse(await ui._handle_source_callback(event, 7, "ui:other"))

    async def test_logout_callback_clears_the_page(self) -> None:
        ui = _UI(FakeCoordinator())
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:source-logout"))
        self.assertTrue(any("退出" in text for _chat, text in ui._client.sent))
        self.assertTrue(event.edits)

    async def test_remove_callback_lists_the_whitelist(self) -> None:
        ui = _UI(FakeCoordinator())
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:source-list"))
        text, rows = event.edits[0]
        self.assertIn("来源白名单", text)
        labels = [button.text for row in rows for button in row]
        self.assertTrue(any("@xiaodeFile_bot" in label for label in labels))
        self.assertTrue(all(len(data) <= 64 for data in _callbacks(rows)))

        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:source-del:0"))
        self.assertIn("已移除来源", event.edits[-1][0])


if __name__ == "__main__":
    unittest.main()
