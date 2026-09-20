from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import unittest
from unittest import mock

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
    caption: str = "",
    video_count: int | None = None,
    photo_count: int | None = None,
    ad_score: int = 0,
    ad_reasons: tuple[str, ...] = (),
) -> SourceMediaSummary:
    if video_count is None:
        video_count = item_count if kinds == (MediaKind.VIDEO,) else 0
    if photo_count is None:
        photo_count = item_count if kinds == (MediaKind.PHOTO,) else 0
    summary = SourceMediaSummary(
        message_id=message_id,
        kinds=kinds,
        item_count=item_count,
        size_bytes=size,
        duration_seconds=duration,
        date=datetime(2026, 9, 19, 3, 30, tzinfo=timezone.utc),
        grouped_id=grouped_id,
        video_count=video_count,
        photo_count=photo_count,
        caption=caption,
        fingerprint=f"{kinds[0].value}|{size}|{message_id}",
        ad_score=ad_score,
        ad_reasons=ad_reasons,
    )
    return summary


class FakeCoordinator:
    active = True

    def __init__(
        self,
        *,
        pages: dict[int, list] | None = None,
        count: int = 2,
        group_ids: list[int] | None = None,
    ) -> None:
        self.pages = {0: [_summary(30506)]} if pages is None else pages
        self.count = count
        self.group_ids = group_ids
        self.grabbed: list[tuple[int, int]] = []
        self.listed: list[tuple[int, int, int, object]] = []
        self.groups: list[tuple[int, int]] = []
        self.selections: list[list[tuple[int, int]]] = []
        self.released: list[tuple[int, str]] = []
        self.refreshes: list[int] = []
        self.more = False
        self.merge_count = 3
        self.merge_failed = 0
        self.merge_accepted = 3
        self.merge_skipped = 1

    def status_line(self) -> str:
        return "已登录 · user_id `7` · 白名单 `2/2` 生效"

    def whitelist(self) -> list[str]:
        return ["@xiaodeFile_bot", "@chunziyuan_bot"]

    def source_label(self, index: int = 0) -> str:
        return "@xiaodeFile_bot"

    def set_awaiting(self, phase) -> None:
        self.phase = phase

    async def list_media(
        self,
        source_index: int = 0,
        *,
        page: int = 0,
        page_size: int = 10,
        since=None,
        refresh: bool = False,
    ):
        self.listed.append((int(source_index), int(page), int(page_size), since))
        if refresh:
            self.refreshes.append(int(source_index))
        # The picker always scans page 0 and slices client-side, so every
        # configured row is returned for the first request.
        items = [entry for group in self.pages.values() for entry in group]
        return (items, self.more, "@xiaodeFile_bot")

    async def release_fingerprint(self, source_index: int, fingerprint: str) -> bool:
        self.released.append((int(source_index), str(fingerprint)))
        return True

    async def group_message_ids(self, source_index: int, message_id: int):
        self.groups.append((int(source_index), int(message_id)))
        if self.group_ids is not None:
            return list(self.group_ids)
        return [int(message_id)]

    async def grab_message(self, source_index: int, message_id: int):
        self.grabbed.append((int(source_index), int(message_id)))
        return (self.count, "@xiaodeFile_bot", self.count, 0)

    async def grab_selection(self, selections):
        self.selections.append([(int(src), int(mid)) for src, mid in selections])
        return (
            self.merge_count,
            "多个来源",
            self.merge_failed,
            self.merge_accepted,
            self.merge_skipped,
        )

    async def logout(self):
        return "已退出登录并删除 session。"

    async def remove_chat(self, index: int) -> str:
        return f"已移除来源 `#{index}`。"


class FakePreviewService:
    def __init__(self, *, image: Path | None = None, failures: int = 0) -> None:
        self.image = image
        self.failures = failures
        self.released: list[str] = []
        self.progress: list[tuple[int, int]] = []
        self.built: list[tuple] = []

    async def build_page(self, source_index, message_ids, *, progress=None):
        self.built.append(("page", int(source_index), tuple(message_ids)))
        total = len(message_ids)
        if progress is not None:
            for done in range(1, total + 1):
                self.progress.append((done, total))
                await progress(done, total)
        fetched = max(0, total - self.failures)
        return SimpleNamespace(
            token="tok",
            image=self.image if fetched else None,
            slots=tuple(True for _ in range(fetched)),
            fetched=fetched,
            total=total,
        )

    async def build_single(self, source_index, message_id, *, progress=None):
        self.built.append(("single", int(source_index), int(message_id)))
        if progress is not None:
            await progress(1, 1)
        return SimpleNamespace(
            token="tok-single",
            image=self.image,
            slots=(True,),
            fetched=1 if self.image else 0,
            total=1,
        )

    def grid_shape(self, count: int):
        columns = min(5, max(1, int(count)))
        return (columns, (int(count) + columns - 1) // columns)

    def release(self, token: str) -> None:
        self.released.append(str(token))


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str, str, list]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self._next = 500

    def _id(self) -> int:
        self._next += 1
        return self._next

    async def send_message(self, chat_id, text, buttons=None):
        self.sent.append((int(chat_id), str(text)))
        return SimpleNamespace(id=self._id())

    async def send_file(self, chat_id, path, caption=None, buttons=None, force_document=False):
        self.photos.append((int(chat_id), str(path), str(caption or ""), list(buttons or [])))
        return SimpleNamespace(id=self._id())

    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((int(chat_id), int(message_id), str(text)))

    async def delete_messages(self, chat_id, message_ids):
        self.deleted.extend((int(chat_id), int(mid)) for mid in message_ids)


class _Event:
    def __init__(self, chat_id: int = 7) -> None:
        self.chat_id = chat_id
        self.edits: list[tuple[str, list]] = []
        self.answers: list[str | None] = []


class _UI(BotUIFormatMixin, BotUISourceMixin):
    def __init__(self, coordinator: FakeCoordinator, service=None, **settings) -> None:
        self._source = coordinator
        self._pick_previews = service
        self._client = FakeClient()
        self._tasks: set[asyncio.Task] = set()
        values = {"source_merge_max_items": 100}
        values.update(settings)
        self._settings = SimpleNamespace(**values)

    async def _edit_page(self, event, text, buttons):
        event.edits.append((text, buttons))

    async def _safe_answer(self, event, text=None, **kwargs):
        event.answers.append(text)


def _callbacks(rows) -> list[bytes]:
    return [button.data for row in rows for button in row if getattr(button, "data", None)]


async def _drain(ui: _UI, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while ui._tasks and time.monotonic() < deadline:
        await asyncio.gather(*list(ui._tasks), return_exceptions=True)


class PickPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_pick_page_shows_composition_numbers_and_short_callbacks(self) -> None:
        summaries = [
            _summary(30506 - index) for index in range(3)
        ] + [
            _summary(
                100,
                kinds=(MediaKind.PHOTO, MediaKind.VIDEO),
                item_count=10,
                size=3 * 1024,
                grouped_id=99,
                video_count=3,
                photo_count=7,
            )
        ]
        ui = _UI(FakeCoordinator(pages={0: summaries}))
        text, rows, visible = await ui._pick_render(7, 0, 0)

        self.assertEqual(len(visible), 4)
        self.assertIn("4)", text)
        self.assertIn("相册 10 项 · 🎬3 🖼7 · 3.0KB", text)
        self.assertIn("🎬 视频 · 1.0KB", text)
        self.assertNotIn("#30506", text)
        self.assertNotIn("🧩", text)
        self.assertNotIn("📥 = 抓取", text)
        self.assertIn("选择要发布的内容 · @xiaodeFile_bot · 第 1 页", text)

        encoded = _callbacks(rows)
        self.assertIn(b"ui:sg:0:30506:0", encoded)
        self.assertIn(b"ui:sv:0:30506:0:1", encoded)
        self.assertIn(b"ui:sk:0:0:30506", encoded)
        self.assertIn(b"ui:pick:0:0", encoded)
        self.assertTrue(all(len(data) <= 64 for data in encoded))
        self.assertEqual(len(rows[0]), 3)

    async def test_pick_page_never_renders_source_captions(self) -> None:
        ad = _summary(500, kinds=(MediaKind.PHOTO,), caption="会长新开VIP群，全是最新最猛的资源")
        ui = _UI(FakeCoordinator(pages={0: [ad]}))
        text, _rows, _visible = await ui._pick_render(7, 0, 0)
        self.assertNotIn("VIP", text)
        self.assertNotIn("资源", text)

    async def test_video_only_filter_hides_photo_only_rows(self) -> None:
        photo = _summary(400, kinds=(MediaKind.PHOTO,))
        video = _summary(401, kinds=(MediaKind.VIDEO,))
        mixed = _summary(
            402,
            kinds=(MediaKind.PHOTO, MediaKind.VIDEO),
            item_count=2,
            video_count=1,
            photo_count=1,
        )
        ui = _UI(FakeCoordinator(pages={0: [photo, video, mixed]}))

        text, _rows, visible = await ui._pick_render(7, 0, 0)
        self.assertEqual(len(visible), 3)

        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:sf:0:0"))
        text, rows, visible = await ui._pick_render(7, 0, 0)
        self.assertEqual([summary.message_id for summary in visible], [401, 402])
        self.assertIn("只看视频", text)
        self.assertIn(b"ui:sf:0:0", _callbacks(rows))

    async def test_window_defaults_to_today_and_can_extend_to_two_days(self) -> None:
        ui = _UI(FakeCoordinator(pages={0: [_summary(1)]}))
        coordinator = ui._source
        await ui._handle_source_callback(_Event(), 7, "ui:pick:0:0")
        first_since = coordinator.listed[-1][3]
        self.assertEqual((first_since.hour, first_since.minute), (0, 0))

        await ui._handle_source_callback(_Event(), 7, "ui:sd:0:0:d")
        two_day_since = coordinator.listed[-1][3]
        self.assertEqual((first_since - two_day_since).days, 1)

        text, rows, _visible = await ui._pick_render(7, 0, 0)
        labels = [button.text for row in rows for button in row]
        self.assertTrue(any("近2天 ✅" in label for label in labels))

        await ui._handle_source_callback(_Event(), 7, "ui:sd:0:0:t")
        self.assertEqual(coordinator.listed[-1][3], first_since)

    async def test_source_switcher_marks_the_current_source(self) -> None:
        ui = _UI(FakeCoordinator(pages={0: [_summary(1)], 1: [_summary(2)]}))
        text, rows, _visible = await ui._pick_render(7, 0, 0)
        labels = [button.text for row in rows for button in row]
        data = _callbacks(rows)
        self.assertIn("✅ @xiaodeFile_bot", labels)
        self.assertIn("@chunziyuan_bot", labels)
        self.assertIn(b"ui:pick:1:0", data)
        self.assertTrue(all(len(item) <= 64 for item in data))

        await ui._handle_source_callback(_Event(), 7, "ui:pick:1:0")
        self.assertEqual(ui._source.listed[-1][0], 1)

    async def test_last_page_has_no_next_button(self) -> None:
        rows_of = [_summary(200 + index) for index in range(25)]
        ui = _UI(FakeCoordinator(pages={0: rows_of}))
        text, rows, visible = await ui._pick_render(7, 0, 2)
        self.assertEqual(len(visible), 5)
        self.assertEqual([summary.message_id for summary in visible][0], 220)
        self.assertIn(b"ui:sp:0:1", _callbacks(rows))
        self.assertNotIn(b"ui:sp:0:3", _callbacks(rows))
        self.assertNotIn("下一页", [button.text for row in rows for button in row])

    async def test_refresh_button_bypasses_the_scan_cache(self) -> None:
        coordinator = FakeCoordinator(pages={0: [_summary(1)]})
        ui = _UI(coordinator)
        await ui._handle_source_callback(_Event(), 7, "ui:pick:0:0")
        self.assertEqual(coordinator.refreshes, [])
        await ui._handle_source_callback(_Event(), 7, "ui:pr:0:0")
        self.assertEqual(coordinator.refreshes, [0])

    async def test_pick_page_offers_previous_and_next_page(self) -> None:
        rows_of = [_summary(100 + index) for index in range(11)]
        ui = _UI(FakeCoordinator(pages={0: rows_of}))
        text, rows, visible = await ui._pick_render(7, 0, 0)
        labels = [button.text for row in rows for button in row]
        self.assertIn("下一页 ➡️", labels)
        self.assertIn(b"ui:sp:0:1", _callbacks(rows))
        self.assertEqual(len(visible), 10)

        text, rows, visible = await ui._pick_render(7, 0, 1)
        self.assertEqual([summary.message_id for summary in visible], [110])
        self.assertIn(b"ui:sp:0:0", _callbacks(rows))
        self.assertNotIn(b"ui:sp:0:2", _callbacks(rows))


class PickGridTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.image = Path(self.tmp.name) / "grid.jpg"
        self.image.write_bytes(b"jpeg-bytes")

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_page_grid_reports_progress_then_sends_the_image(self) -> None:
        service = FakePreviewService(image=self.image)
        coordinator = FakeCoordinator(
            pages={0: [_summary(10 + index) for index in range(4)]}
        )
        ui = _UI(coordinator, service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await _drain(ui)

        self.assertTrue(service.progress)
        self.assertEqual(service.progress[-1], (4, 4))
        progress_texts = [text for _chat, _mid, text in ui._client.edits if "缩略图" in text]
        self.assertGreaterEqual(len(progress_texts), 2, progress_texts)
        self.assertTrue(any("0/4" in text for _chat, text in ui._client.sent), ui._client.sent)
        self.assertIn("4/4", progress_texts[-1])
        self.assertEqual(len(ui._client.photos), 1)
        chat, path, caption, buttons = ui._client.photos[0]
        self.assertEqual(path, str(self.image))
        self.assertIn("缩略图", caption)
        self.assertTrue(all(len(getattr(b, "data", b"") or b"") <= 64 for row in buttons for b in row))
        self.assertEqual(service.released, ["tok"])
        self.assertTrue(ui._client.deleted)

    async def test_second_page_grid_replaces_the_previous_message(self) -> None:
        service = FakePreviewService(image=self.image)
        coordinator = FakeCoordinator(
            pages={0: [_summary(100 + index) for index in range(11)]}
        )
        ui = _UI(coordinator, service)
        await ui._handle_source_callback(_Event(), 7, "ui:pick:0:0")
        await _drain(ui)
        first_id = ui._pick_grid_messages()[7]
        await ui._handle_source_callback(_Event(), 7, "ui:pick:0:1")
        await _drain(ui)
        self.assertIn((7, first_id), ui._client.deleted)
        self.assertNotEqual(ui._pick_grid_messages()[7], first_id)

    async def test_missing_thumbnails_keep_a_visible_warning(self) -> None:
        service = FakePreviewService(image=None, failures=3)
        coordinator = FakeCoordinator(pages={0: [_summary(10), _summary(11), _summary(12)]})
        ui = _UI(coordinator, service)
        await ui._handle_source_callback(_Event(), 7, "ui:pick:0:0")
        await _drain(ui)
        self.assertEqual(ui._client.photos, [])
        warnings = [text for _chat, _mid, text in ui._client.edits if "没有取到缩略图" in text]
        self.assertTrue(warnings, ui._client.edits)
        self.assertIn("0/3", warnings[-1])

    async def test_grid_failure_with_thumbnails_has_its_own_warning(self) -> None:
        service = FakePreviewService(image=None, failures=1)
        coordinator = FakeCoordinator(
            pages={0: [_summary(10), _summary(11), _summary(12)]}
        )
        ui = _UI(coordinator, service)
        await ui._handle_source_callback(_Event(), 7, "ui:pick:0:0")
        await _drain(ui)
        self.assertEqual(ui._client.photos, [])
        warnings = [text for _chat, _mid, text in ui._client.edits if "预览图生成失败" in text]
        self.assertTrue(warnings, ui._client.edits)
        self.assertIn("2/3", warnings[-1])

    async def test_grid_requests_run_without_a_service(self) -> None:
        ui = _UI(FakeCoordinator(pages={0: [_summary(10)]}), None)
        await ui._handle_source_callback(_Event(), 7, "ui:pick:0:0")
        await _drain(ui)
        self.assertEqual(ui._client.photos, [])


class PickPreviewButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.image = Path(self.tmp.name) / "thumb.jpg"
        self.image.write_bytes(b"jpeg-bytes")

    async def asyncTearDown(self) -> None:
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        self.tmp.cleanup()

    async def test_single_preview_sends_photo_with_metadata(self) -> None:
        service = FakePreviewService(image=self.image)
        coordinator = FakeCoordinator(
            pages={0: [_summary(30506, kinds=(MediaKind.VIDEO,), size=2048)]}
        )
        ui = _UI(coordinator, service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await ui._handle_source_callback(event, 7, "ui:sv:0:30506:0")

        self.assertEqual(service.built[-1], ("page", 0, (30506,)))
        self.assertEqual(len(ui._client.photos), 1)
        chat, path, caption, buttons = ui._client.photos[0]
        self.assertIn("🎬 视频 · 2.0KB", caption)
        self.assertIn("位置对应组内第 1–1 项", caption)
        self.assertTrue(all(len(data) <= 64 for data in _callbacks(buttons)))
        self.assertEqual(service.released, ["tok"])
        self.assertIn(b"ui:sg:0:30506:0", _callbacks(buttons))

    async def test_group_preview_uses_every_message_of_the_album(self) -> None:
        service = FakePreviewService(image=self.image)
        coordinator = FakeCoordinator(
            pages={
                0: [
                    _summary(
                        30506,
                        kinds=(MediaKind.VIDEO, MediaKind.PHOTO),
                        item_count=3,
                        video_count=2,
                        photo_count=1,
                    )
                ]
            },
            group_ids=[30506, 30507, 30508],
        )
        ui = _UI(coordinator, service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await ui._handle_source_callback(event, 7, "ui:sv:0:30506:0")

        self.assertEqual(coordinator.groups, [(0, 30506)])
        self.assertEqual(service.built[-1], ("page", 0, (30506, 30507, 30508)))
        caption = ui._client.photos[-1][2]
        self.assertIn("位置对应组内第 1–3 项", caption)

    async def test_single_preview_without_thumbnail_warns(self) -> None:
        service = FakePreviewService(image=None)
        ui = _UI(FakeCoordinator(pages={0: [_summary(30506)]}), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await ui._handle_source_callback(event, 7, "ui:sv:0:30506:0")
        self.assertEqual(ui._client.photos, [])
        self.assertTrue(
            any("整组预览生成失败" in text for _chat, _mid, text in ui._client.edits)
        )


class PreviewAccumulationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.image = Path(self.tmp.name) / "thumb.jpg"
        self.image.write_bytes(b"jpeg-bytes")

    async def asyncTearDown(self) -> None:
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        self.tmp.cleanup()

    def _pages(self):
        return {
            0: [
                _summary(30506, kinds=(MediaKind.VIDEO,), size=2048),
                _summary(30507, kinds=(MediaKind.PHOTO,), size=4096),
            ]
        }

    def _tracked(self, ui) -> list[int]:
        return list(ui._pick_preview_messages().get(7, []))

    async def _open(self, ui, event, message_id: int, position: int) -> int:
        before = set(self._tracked(ui))
        await ui._handle_source_callback(event, 7, f"ui:sv:0:{message_id}:0:{position}")
        added = [mid for mid in self._tracked(ui) if mid not in before]
        self.assertEqual(len(added), 1, self._tracked(ui))
        return added[0]

    async def test_previews_accumulate_instead_of_replacing_each_other(self) -> None:
        service = FakePreviewService(image=self.image)
        ui = _UI(FakeCoordinator(pages=self._pages()), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        first = await self._open(ui, event, 30506, 1)
        ui._client.deleted.clear()
        second = await self._open(ui, event, 30507, 2)

        self.assertEqual(len(ui._client.photos), 2)
        self.assertNotIn((7, first), ui._client.deleted)
        self.assertEqual(self._tracked(ui), [first, second])

    async def test_preview_caption_carries_the_row_position(self) -> None:
        service = FakePreviewService(image=self.image)
        ui = _UI(FakeCoordinator(pages=self._pages()), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await ui._handle_source_callback(event, 7, "ui:sv:0:30507:0:2")
        caption = ui._client.photos[-1][2]
        self.assertIn("👁 第 2 项 · 🖼 图片 · 4.0KB", caption)

    async def test_selection_destroys_every_accumulated_preview(self) -> None:
        service = FakePreviewService(image=self.image)
        ui = _UI(FakeCoordinator(pages=self._pages()), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        first = await self._open(ui, event, 30506, 1)
        second = await self._open(ui, event, 30507, 2)
        ui._client.deleted.clear()

        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")
        self.assertEqual(ui._client.deleted, [(7, first), (7, second)])
        self.assertFalse(self._tracked(ui))

    async def test_manual_button_reports_and_clears(self) -> None:
        service = FakePreviewService(image=self.image)
        ui = _UI(FakeCoordinator(pages=self._pages()), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        first = await self._open(ui, event, 30506, 1)
        second = await self._open(ui, event, 30507, 2)

        _text, rows, _visible = await ui._pick_render(7, 0, 0)
        self.assertIn("🧹 清理预览 (2)", [button.text for row in rows for button in row])
        self.assertIn(b"ui:pc:0:0", _callbacks(rows))

        ui._client.deleted.clear()
        await ui._handle_source_callback(event, 7, "ui:pc:0:0")
        self.assertEqual(ui._client.deleted, [(7, first), (7, second)])
        self.assertFalse(self._tracked(ui))
        self.assertTrue(any("已清理 2 张预览" in str(answer) for answer in event.answers))

    async def test_cleanup_button_disappears_when_nothing_is_tracked(self) -> None:
        ui = _UI(FakeCoordinator(pages=self._pages()))
        _text, rows, _visible = await ui._pick_render(7, 0, 0)
        self.assertNotIn("清理预览", [button.text for row in rows for button in row])

    async def test_cap_deletes_the_oldest_preview(self) -> None:
        service = FakePreviewService(image=self.image)
        rows = [
            _summary(40000 + index, kinds=(MediaKind.PHOTO,), size=1024)
            for index in range(12)
        ]
        ui = _UI(FakeCoordinator(pages={0: rows}), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        first = await self._open(ui, event, 40000, 1)
        second = await self._open(ui, event, 40001, 2)
        for index in range(2, 12):
            await self._open(ui, event, 40000 + index, index + 1)

        tracked = self._tracked(ui)
        self.assertEqual(len(tracked), 10)
        self.assertIn((7, first), ui._client.deleted)
        self.assertIn((7, second), ui._client.deleted)
        self.assertNotIn(first, tracked)

    async def test_expiry_only_deletes_a_tracked_preview(self) -> None:
        service = FakePreviewService(image=self.image)
        ui = _UI(FakeCoordinator(pages=self._pages()), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        tracked = await self._open(ui, event, 30506, 1)
        ui._client.deleted.clear()

        with mock.patch(
            "tgvio.adapters.telegram.bot_ui_source._PREVIEW_TTL_SECONDS", 0.01
        ):
            await ui._expire_preview(7, 7, tracked)
            self.assertIn((7, tracked), ui._client.deleted)
            self.assertFalse(self._tracked(ui))

            ui._client.deleted.clear()
            await ui._expire_preview(7, 7, tracked)
            self.assertNotIn((7, tracked), ui._client.deleted)

    async def test_source_switch_clears_previews(self) -> None:
        service = FakePreviewService(image=self.image)
        ui = _UI(FakeCoordinator(pages=self._pages()), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        first = await self._open(ui, event, 30506, 1)
        second = await self._open(ui, event, 30507, 2)
        ui._client.deleted.clear()

        await ui._handle_source_callback(event, 7, "ui:pick:1:0")
        self.assertFalse(self._tracked(ui))
        self.assertEqual(ui._client.deleted, [(7, first), (7, second)])

    async def test_refresh_clears_previews(self) -> None:
        service = FakePreviewService(image=self.image)
        ui = _UI(FakeCoordinator(pages=self._pages()), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        tracked = await self._open(ui, event, 30506, 1)
        ui._client.deleted.clear()

        await ui._handle_source_callback(event, 7, "ui:pr:0:0")
        self.assertIn((7, tracked), ui._client.deleted)
        self.assertFalse(self._tracked(ui))
        self.assertEqual(ui._source.refreshes, [0])

    async def test_paging_keeps_the_previews(self) -> None:
        service = FakePreviewService(image=self.image)
        rows = [
            _summary(50000 + index, kinds=(MediaKind.PHOTO,), size=1024)
            for index in range(11)
        ]
        ui = _UI(FakeCoordinator(pages={0: rows}), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        tracked = await self._open(ui, event, 50000, 1)
        ui._client.deleted.clear()

        await ui._handle_source_callback(event, 7, "ui:sp:0:1")
        self.assertNotIn((7, tracked), ui._client.deleted)
        self.assertEqual(self._tracked(ui), [tracked])

    async def test_publish_flow_clears_previews(self) -> None:
        service = FakePreviewService(image=self.image)
        ui = _UI(FakeCoordinator(pages=self._pages()), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await self._open(ui, event, 30506, 1)
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")
        tracked = await self._open(ui, event, 30507, 2)
        ui._client.deleted.clear()

        await ui._handle_source_callback(event, 7, "ui:sz:0:0")
        self.assertEqual(ui._client.deleted, [(7, tracked)])
        self.assertFalse(self._tracked(ui))


class MergeSelectionTests(unittest.IsolatedAsyncioTestCase):
    def _pages(self):
        return {
            0: [
                _summary(30506, kinds=(MediaKind.VIDEO,), item_count=10, video_count=10),
                _summary(30507, kinds=(MediaKind.PHOTO,), item_count=4, photo_count=4),
            ]
        }

    async def test_toggle_selection_updates_header_and_buttons(self) -> None:
        coordinator = FakeCoordinator(pages=self._pages())
        ui = _UI(coordinator)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")

        text, rows = event.edits[-1]
        self.assertIn("已选 1 组/10 项", text)
        labels = [button.text for row in rows for button in row]
        self.assertIn("✅ 1", labels)
        self.assertIn("☑️ 2", labels)
        self.assertIn("✅ 发布已选 (1)", labels)
        data = _callbacks(rows)
        self.assertIn(b"ui:sz:0:0", data)
        self.assertIn(b"ui:sx:0:0", data)
        self.assertTrue(all(len(item) <= 64 for item in data))

    async def test_toggle_off_and_clear_reset_the_selection(self) -> None:
        ui = _UI(FakeCoordinator(pages=self._pages()))
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")
        self.assertEqual(ui._selection_summary(7)["rows"], 0)

        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30507")
        self.assertEqual(ui._selection_summary(7)["rows"], 2)
        await ui._handle_source_callback(event, 7, "ui:sx:0:0")
        self.assertEqual(ui._selection_summary(7)["rows"], 0)

    async def test_merge_confirm_card_shows_totals_without_publishing(self) -> None:
        coordinator = FakeCoordinator(pages=self._pages())
        ui = _UI(coordinator)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30507")
        await ui._handle_source_callback(event, 7, "ui:sz:0:0")

        text, rows = event.edits[-1]
        self.assertIn("合并发布", text)
        self.assertIn("共 2 组 · 14 项 · 🎬10 🖼4", text)
        self.assertEqual(coordinator.selections, [])
        data = _callbacks(rows)
        self.assertIn(b"ui:sm:0:0", data)
        self.assertIn(b"ui:spm:0:0", data)

    async def test_publish_merged_submits_in_order_and_reports_skips(self) -> None:
        coordinator = FakeCoordinator(pages=self._pages())
        ui = _UI(coordinator)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30507")
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")
        await ui._handle_source_callback(event, 7, "ui:sm:0:0")

        self.assertEqual(coordinator.selections, [[(0, 30507), (0, 30506)]])
        self.assertEqual(ui._selection_summary(7)["rows"], 0)
        ack = "\n".join(text for _chat, text in ui._client.sent)
        self.assertIn("已合并提交", ack)
        self.assertIn("跳过", ack)
        self.assertTrue(event.edits)

    async def test_merge_limit_blocks_publishing(self) -> None:
        coordinator = FakeCoordinator(pages=self._pages())
        ui = _UI(coordinator, source_merge_max_items=5)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:pick:0:0")
        await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")
        await ui._handle_source_callback(event, 7, "ui:sz:0:0")
        self.assertIn("超过一次上限", event.edits[-1][0])

        await ui._handle_source_callback(event, 7, "ui:sm:0:0")
        self.assertEqual(coordinator.selections, [])
        self.assertIn("超过一次上限，请分批", event.answers)

    async def test_merged_preview_uses_every_selected_group(self) -> None:
        with TemporaryDirectory() as tmp:
            image = Path(tmp) / "merge.jpg"
            image.write_bytes(b"jpeg-bytes")
            service = FakePreviewService(image=image)
            coordinator = FakeCoordinator(pages=self._pages(), group_ids=[1, 2])
            ui = _UI(coordinator, service)
            event = _Event()
            await ui._handle_source_callback(event, 7, "ui:pick:0:0")
            await ui._handle_source_callback(event, 7, "ui:sk:0:0:30506")
            await ui._handle_source_callback(event, 7, "ui:spm:0:0")

            self.assertEqual(coordinator.groups, [(0, 30506)])
            self.assertEqual(service.built[-1], ("page", 0, (1, 2)))
            self.assertTrue(ui._client.photos)
            self.assertIn("合并预览", ui._client.photos[-1][2])
            for task in list(ui._tasks):
                task.cancel()


class AdFilterUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.image = Path(self.tmp.name) / "grid.jpg"
        self.image.write_bytes(b"jpeg-bytes")

    async def asyncTearDown(self) -> None:
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        self.tmp.cleanup()

    def _pages(self):
        return {
            0: [
                _summary(
                    30506,
                    kinds=(MediaKind.PHOTO,),
                    size=116647,
                    caption="会长新开VIP群",
                    ad_score=65,
                    ad_reasons=("lone_photo", "same_content_x2"),
                ),
                _summary(30507, kinds=(MediaKind.VIDEO,), size=5_000_000),
            ]
        }

    async def test_pick_page_hides_ads_and_offers_the_hidden_view(self) -> None:
        ui = _UI(FakeCoordinator(pages=self._pages()))
        text, rows, visible = await ui._pick_render(7, 0, 0)
        self.assertEqual([summary.message_id for summary in visible], [30507])
        self.assertIn("已隐藏 1 个疑似广告", text)
        data = _callbacks(rows)
        self.assertIn(b"ui:sh:0:0", data)
        self.assertIn(b"ui:sa:0:0", data)
        self.assertTrue(all(len(item) <= 64 for item in data))

    async def test_hidden_page_shows_reasons_and_release_buttons(self) -> None:
        ui = _UI(FakeCoordinator(pages=self._pages()))
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:sh:0:0"))
        text, rows = event.edits[-1]
        self.assertIn("被隐藏的疑似广告", text)
        self.assertIn("疑似广告（孤立图片 · 同一内容出现 2 次）", text)
        data = _callbacks(rows)
        self.assertIn(b"ui:sr:0:30506:0", data)
        self.assertIn(b"ui:sg:0:30506:0", data)
        self.assertIn(b"ui:pick:0:0", data)
        self.assertTrue(all(len(item) <= 64 for item in data))

    async def test_release_moves_the_row_back_to_the_list(self) -> None:
        coordinator = FakeCoordinator(pages=self._pages())
        service = FakePreviewService(image=self.image)
        ui = _UI(coordinator, service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:sh:0:0")
        await ui._handle_source_callback(event, 7, "ui:sr:0:30506:0")

        self.assertEqual(len(coordinator.released), 1)
        self.assertEqual(coordinator.released[0][0], 0)
        self.assertTrue(coordinator.released[0][1])
        self.assertTrue(any("已放行" in text for _chat, text in ui._client.sent))

    async def test_toggle_shows_everything_again(self) -> None:
        ui = _UI(FakeCoordinator(pages=self._pages()))
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:sa:0:0"))
        self.assertFalse(ui._ads_hidden_enabled(7))
        text, _rows, visible = await ui._pick_render(7, 0, 0)
        self.assertEqual(len(visible), 2)
        self.assertNotIn("已隐藏", text)

    async def test_hidden_page_builds_a_thumbnail_grid(self) -> None:
        service = FakePreviewService(image=self.image)
        ui = _UI(FakeCoordinator(pages=self._pages()), service)
        event = _Event()
        await ui._handle_source_callback(event, 7, "ui:sh:0:0")
        await _drain(ui)
        self.assertTrue(service.built)
        self.assertEqual(service.built[-1][2], (30506,))


class PickConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def test_pick_button_only_opens_a_confirmation(self) -> None:
        coordinator = FakeCoordinator(pages={0: [_summary(30506)]})
        ui = _UI(coordinator)
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:sg:0:30506:0"))
        text, rows = event.edits[0]
        self.assertIn("确认抓取", text)
        self.assertEqual(coordinator.grabbed, [])
        encoded = _callbacks(rows)
        self.assertIn(b"ui:sy:0:30506:0", encoded)
        self.assertIn(b"ui:sn:0:0", encoded)
        self.assertTrue(all(len(data) <= 64 for data in encoded))

    async def test_confirmation_then_grabs_and_returns_to_the_list(self) -> None:
        coordinator = FakeCoordinator(pages={0: [_summary(30506)]})
        ui = _UI(coordinator)
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:sy:0:30506:0"))
        self.assertEqual(coordinator.grabbed, [(0, 30506)])
        self.assertTrue(any("已抓取" in text for _chat, text in ui._client.sent))
        self.assertTrue(event.edits)
        self.assertIn("选择要发布的内容", event.edits[-1][0])

    async def test_cancel_returns_to_the_list_without_grabbing(self) -> None:
        coordinator = FakeCoordinator(pages={0: [_summary(30506)]})
        ui = _UI(coordinator)
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:sn:0:0"))
        self.assertEqual(coordinator.grabbed, [])
        self.assertTrue(event.edits)

    async def test_unknown_action_is_not_handled(self) -> None:
        ui = _UI(FakeCoordinator())
        self.assertFalse(await ui._handle_source_callback(_Event(), 7, "ui:other"))


class SourcePageTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_page_links_to_the_picker(self) -> None:
        ui = _UI(FakeCoordinator())
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:source"))
        text, rows = event.edits[0]
        self.assertIn("来源账号", text)
        self.assertIn(b"ui:pick:0:0", _callbacks(rows))

    async def test_remove_callback_lists_the_whitelist(self) -> None:
        ui = _UI(FakeCoordinator())
        event = _Event()
        self.assertTrue(await ui._handle_source_callback(event, 7, "ui:source-list"))
        text, rows = event.edits[0]
        labels = [button.text for row in rows for button in row]
        self.assertTrue(any("@xiaodeFile_bot" in label for label in labels))
        self.assertTrue(all(len(data) <= 64 for data in _callbacks(rows)))


if __name__ == "__main__":
    unittest.main()
