"""The "already submitted" side of the source picker.

Rows whose source messages already belong to a live job are hidden from the pick
list: the owner asked that once something is submitted it is never grabbed
again. They stay inspectable through a read-only page (thumbnails with the same
1-N numbering) and come back automatically when the job failed or was cancelled.
"""

from __future__ import annotations

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.bot_ui_source_ads import _PICK_SCAN_ITEMS

_SUBMITTED_PAGE_SIZE = 10


class BotUISourceDoneMixin:
    def _pick_submitted_cache(self) -> dict:
        state = getattr(self, "_pick_submitted_rows", None)
        if state is None:
            state = {}
            self._pick_submitted_rows = state
        return state

    def _row_submitted(
        self,
        owner_id: int,
        source_index: int,
        message_id: int,
        page: int,
    ) -> bool:
        """Whether a row already belongs to a live job.

        Hidden rows are not in the page cache any more, so the submitted rows of
        the last scan are checked as well.
        """

        summary = self._pick_cache().get(
            (int(owner_id), int(source_index), int(page)), {}
        ).get(int(message_id))
        if getattr(summary, "already_submitted", False):
            return True
        rows = self._pick_submitted_cache().get((int(owner_id), int(source_index)))
        if not rows:
            return False
        return any(
            int(getattr(row, "message_id", 0)) == int(message_id) for row in rows
        )

    async def _submitted_rows(self, owner_id: int, source_index: int) -> list:
        """Hidden (already submitted) rows of one source, newest first."""

        key = (int(owner_id), int(source_index))
        cached = self._pick_submitted_cache().get(key)
        if cached is not None:
            return list(cached)
        coordinator = self._source_coordinator()
        if coordinator is None:
            return []
        summaries, _has_more, _label = await coordinator.list_media(
            source_index,
            page=0,
            page_size=_PICK_SCAN_ITEMS,
            since=self._window_since(owner_id),
        )
        rows = [summary for summary in summaries if getattr(summary, "already_submitted", False)]
        self._pick_submitted_cache()[key] = list(rows)
        return rows

    async def _submitted_render(
        self,
        owner_id: int,
        source_index: int = 0,
        page: int = 0,
    ) -> tuple[str, list, list]:
        """Read-only page: what was already submitted for this source."""

        coordinator = self._source_coordinator()
        if coordinator is None:
            return ("来源功能未装配。", [[Button.inline("🏠 首页", b"ui:home")]], [])
        rows_of = await self._submitted_rows(owner_id, source_index)
        page = max(0, int(page))
        start = page * _SUBMITTED_PAGE_SIZE
        window = rows_of[start : start + _SUBMITTED_PAGE_SIZE]
        label = coordinator.source_label(int(source_index)) or "未配置"
        lines = [
            f"✅ 已提交（不再抓取） · {label} · 第 {page + 1} 页",
            "──────────",
        ]
        if not window:
            lines.append("这个来源还没有已提交的条目。")
        for position, summary in enumerate(window, start=1):
            lines.append(f"{position}) {self._summary_line(summary)}")
        lines.append("──────────")
        lines.append("这些内容已经入过队，不会再出现在选片列表里；失败或取消的任务会自动回到列表。")
        rows: list[list] = []
        nav: list = []
        if page > 0:
            nav.append(
                Button.inline("⬅️ 上一页", f"ui:ps:{int(source_index)}:{page - 1}".encode())
            )
        if len(rows_of) > start + _SUBMITTED_PAGE_SIZE:
            nav.append(
                Button.inline("下一页 ➡️", f"ui:ps:{int(source_index)}:{page + 1}".encode())
            )
        if nav:
            rows.append(nav)
        rows.append(
            [
                Button.inline("⬅️ 返回列表", f"ui:pick:{int(source_index)}:0".encode()),
                Button.inline("🏠 首页", b"ui:home"),
            ]
        )
        return "\n".join(lines), rows, window

    async def _show_submitted_callback(
        self,
        event,
        owner_id: int,
        source_index: int,
        page: int,
    ) -> None:
        text, rows, window = await self._submitted_render(owner_id, source_index, page)
        await self._edit_page(event, text, rows)
        if window:
            self._start_page_grid(
                owner_id,
                int(event.chat_id),
                source_index,
                page,
                [summary.message_id for summary in window],
            )
