"""Advertisement filtering for the source picker.

The pick list hides rows whose confidence score says "advertisement" (see
``domain/ad_filter.py``). Everything stays inspectable: the owner can open the
hidden list, see the thumbnails, read why each row was hidden and release it -
a released row is remembered per chat so it never disappears again.
"""

from __future__ import annotations

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.domain.ad_filter import reason_label

_PICK_SCAN_ITEMS = 400
_HIDDEN_PAGE_SIZE = 10


class BotUISourceAdsMixin:
    def _pick_ad_filter(self) -> dict[int, bool]:
        state = getattr(self, "_pick_ad_filters", None)
        if state is None:
            state = {}
            self._pick_ad_filters = state
        return state

    def _ads_hidden_enabled(self, owner_id: int) -> bool:
        return bool(self._pick_ad_filter().get(int(owner_id), True))

    def _pick_hidden_cache(self) -> dict:
        state = getattr(self, "_pick_hidden_rows", None)
        if state is None:
            state = {}
            self._pick_hidden_rows = state
        return state

    @staticmethod
    def _ad_row_label(summary) -> str:
        reasons = reason_label(tuple(getattr(summary, "ad_reasons", ()) or ()))
        return f"疑似广告（{reasons}）" if reasons else "疑似广告"

    async def _pick_hidden_render(
        self,
        owner_id: int,
        source_index: int = 0,
        page: int = 0,
    ) -> tuple[str, list, list]:
        """Render the hidden (suspected ad) rows with their thumbnails."""

        coordinator = self._source_coordinator()
        if coordinator is None:
            return ("来源功能未装配。", [[Button.inline("🏠 首页", b"ui:home")]], [])
        hidden = await self._hidden_rows(owner_id, source_index)
        page = max(0, int(page))
        start = page * _HIDDEN_PAGE_SIZE
        window = hidden[start : start + _HIDDEN_PAGE_SIZE]
        lines = [
            f"🚫 被隐藏的疑似广告 · {await self._hidden_source_label(source_index)}"
            f" · 第 {page + 1} 页",
            "──────────",
        ]
        if not window:
            lines.append("没有更多被隐藏的条目了。")
        for position, summary in enumerate(window, start=1):
            lines.append(
                f"{position}) {self._summary_line(summary)} · {self._ad_row_label(summary)}"
            )
        lines.append("──────────")
        lines.append("放行后这条会回到列表，并且之后不再隐藏。")
        rows: list[list] = []
        for position, summary in enumerate(window, start=1):
            rows.append(
                [
                    Button.inline(
                        f"✅ 放行 {position}",
                        f"ui:sr:{int(source_index)}:{summary.message_id}:{page}".encode(),
                    ),
                    Button.inline(
                        f"📥 抓取 {position}",
                        f"ui:sg:{int(source_index)}:{summary.message_id}:{page}".encode(),
                    ),
                ]
            )
        nav: list = []
        if page > 0:
            nav.append(Button.inline("⬅️ 上一页", f"ui:sh:{int(source_index)}:{page - 1}".encode()))
        if len(hidden) > start + _HIDDEN_PAGE_SIZE:
            nav.append(Button.inline("下一页 ➡️", f"ui:sh:{int(source_index)}:{page + 1}".encode()))
        if nav:
            rows.append(nav)
        rows.append(
            [
                Button.inline("⬅️ 返回列表", f"ui:pick:{int(source_index)}:0".encode()),
                Button.inline(
                    "🔛 显示全部" if self._ads_hidden_enabled(owner_id) else "🚫 过滤广告",
                    f"ui:sa:{int(source_index)}:0".encode(),
                ),
            ]
        )
        rows.append([Button.inline("🏠 首页", b"ui:home")])
        return "\n".join(lines), rows, window

    async def _hidden_source_label(self, source_index: int) -> str:
        coordinator = self._source_coordinator()
        label = coordinator.source_label(int(source_index)) if coordinator is not None else ""
        return label or "未配置"

    async def _hidden_rows(self, owner_id: int, source_index: int) -> list:
        """Hidden rows of one source, reusing a fresh cache when possible."""

        coordinator = self._source_coordinator()
        if coordinator is None:
            return []
        key = (int(owner_id), int(source_index))
        cached = self._pick_hidden_cache().get(key)
        if cached is not None:
            return list(cached)
        summaries, _has_more, _label = await coordinator.list_media(
            source_index,
            page=0,
            page_size=_PICK_SCAN_ITEMS,
            since=self._window_since(owner_id),
        )
        hidden = [summary for summary in summaries if getattr(summary, "is_ad", False)]
        self._pick_hidden_cache()[key] = list(hidden)
        return hidden

    async def _show_hidden_callback(
        self,
        event,
        owner_id: int,
        source_index: int,
        page: int,
    ) -> None:
        text, rows, window = await self._pick_hidden_render(owner_id, source_index, page)
        await self._edit_page(event, text, rows)
        if window:
            self._start_page_grid(
                owner_id,
                int(event.chat_id),
                source_index,
                page,
                [summary.message_id for summary in window],
            )

    async def _toggle_ad_filter(
        self,
        event,
        owner_id: int,
        source_index: int,
        page: int,
    ) -> None:
        state = self._pick_ad_filter()
        state[int(owner_id)] = not bool(state.get(int(owner_id), True))
        self._pick_hidden_cache().pop((int(owner_id), int(source_index)), None)
        await self._safe_answer(
            event,
            "已隐藏疑似广告" if state[int(owner_id)] else "已显示全部条目",
        )
        await self._show_pick_callback(
            event, owner_id, source_index, max(0, page), with_grid=False
        )

    async def _release_ad_callback(
        self,
        event,
        owner_id: int,
        source_index: int,
        message_id: int,
        page: int,
    ) -> None:
        coordinator = self._source_coordinator()
        if coordinator is None:
            await self._safe_answer(event, "来源功能不可用", alert=True)
            return
        hidden = await self._hidden_rows(owner_id, source_index)
        summary = next(
            (entry for entry in hidden if int(entry.message_id) == int(message_id)),
            None,
        )
        fingerprint = str(getattr(summary, "fingerprint", "") or "")
        if not fingerprint:
            await self._safe_answer(event, "这条已刷新，请重试", alert=True)
            return
        await coordinator.release_fingerprint(source_index, fingerprint)
        self._pick_hidden_cache().pop((int(owner_id), int(source_index)), None)
        await self._safe_answer(event, "已放行")
        await self._send_text(
            int(event.chat_id),
            "✅ 已放行，之后不再隐藏这条内容；回到列表即可抓取。",
        )
        await self._show_hidden_callback(event, owner_id, source_index, page)
