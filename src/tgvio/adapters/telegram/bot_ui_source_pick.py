"""The pick list page: rows, filters and the page thumbnail grid.

Kept apart from the source-setup UI so the dispatch file stays small; the mixin
only renders and never touches the queue directly.
"""

from __future__ import annotations

import asyncio

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.bot_ui_source_ads import _PICK_SCAN_ITEMS

_PICK_PAGE_SIZE = 10


class BotUISourcePickMixin:

    # ------------------------------------------------------------- pick page
    async def _pick_render(
        self,
        owner_id: int,
        source_index: int = 0,
        page: int = 0,
        *,
        refresh: bool = False,
    ) -> tuple[str, list, list]:
        """Render one pick page: text, buttons and the visible summaries."""

        coordinator = self._source_coordinator()
        if coordinator is None:
            return ("来源功能未装配。", [[Button.inline("🏠 首页", b"ui:home")]], [])
        scan, _more_rows, label = await coordinator.list_media(
            source_index,
            page=0,
            page_size=_PICK_SCAN_ITEMS,
            since=self._window_since(owner_id),
            refresh=refresh,
        )
        self._pick_sources()[int(owner_id)] = int(source_index)
        self._pick_pages()[int(owner_id)] = (int(source_index), max(0, int(page)))
        scan = list(scan)
        submitted = [summary for summary in scan if getattr(summary, "already_submitted", False)]
        self._pick_submitted_cache()[(int(owner_id), int(source_index))] = list(submitted)
        hidden = [
            summary
            for summary in scan
            if getattr(summary, "is_ad", False) and not getattr(summary, "already_submitted", False)
        ]
        self._pick_hidden_cache()[(int(owner_id), int(source_index))] = list(hidden)
        ads_hidden = self._ads_hidden_enabled(owner_id)
        visible = [s for s in scan if not getattr(s, "already_submitted", False)]
        visible = [s for s in visible if not (ads_hidden and getattr(s, "is_ad", False))]
        video_only = bool(self._pick_filter().get(int(owner_id)))
        if video_only:
            visible = [summary for summary in visible if summary.has_video]
        start = max(0, int(page)) * _PICK_PAGE_SIZE
        summaries = visible[start : start + _PICK_PAGE_SIZE]
        # Only the rows we actually analysed can be paged through, otherwise a
        # "next page" button would lead to pages that can never be filled.
        has_more = len(visible) > start + _PICK_PAGE_SIZE
        self._pick_cache()[(int(owner_id), int(source_index), int(page))] = {
            summary.message_id: summary for summary in summaries
        }
        window = self._pick_window().get(int(owner_id), "today")
        header = f"选择要发布的内容 · {label or '未配置'} · 第 {int(page) + 1} 页"
        if video_only:
            header += " · 只看视频"
        if hidden:
            if ads_hidden:
                header += f" · 已隐藏 {len(hidden)} 个疑似广告"
            else:
                header += f" · {len(hidden)} 个疑似广告（已显示）"
        if submitted:
            header += f" · 已提交 {len(submitted)} 条（不再抓取）"
        selection = self._selection_summary(owner_id)
        if selection["rows"]:
            header += f" · 已选 {selection['rows']} 组/{selection['items']} 项"
        lines = [header, "──────────"]
        rows: list[list] = []
        if not summaries:
            lines.append("这一页没有符合条件的媒体（可以翻页或关掉筛选）。")
        for position, summary in enumerate(summaries, start=1):
            lines.append(f"{position}) {self._summary_line(summary)}")
            selected = self._selection_key(source_index, summary.message_id) in (
                self._selection_for(owner_id)["meta"]
            )
            rows.append(
                [
                    Button.inline(
                        f"📥 {position}",
                        f"ui:sg:{int(source_index)}:{summary.message_id}:{int(page)}".encode(),
                    ),
                    Button.inline(
                        f"👁 {position}",
                        f"ui:sv:{int(source_index)}:{summary.message_id}:{int(page)}:{position}".encode(),
                    ),
                    Button.inline(
                        ("✅ " if selected else "☑️ ") + str(position),
                        f"ui:sk:{int(source_index)}:{int(page)}:{summary.message_id}".encode(),
                    ),
                ]
            )
        lines.append("──────────")
        nav: list = []
        if page > 0:
            nav.append(Button.inline("⬅️ 上一页", f"ui:sp:{int(source_index)}:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline("下一页 ➡️", f"ui:sp:{int(source_index)}:{page + 1}".encode()))
        if nav:
            rows.append(nav)
        if selection["rows"]:
            rows.append(
                [
                    Button.inline(
                        f"✅ 发布已选 ({selection['rows']})",
                        f"ui:sz:{int(source_index)}:{int(page)}".encode(),
                    ),
                    Button.inline(
                        "🧹 清空",
                        f"ui:sx:{int(source_index)}:{int(page)}".encode(),
                    ),
                ]
            )
        source_row: list = []
        whitelist = coordinator.whitelist()
        for index, entry in enumerate(whitelist[:5]):
            prefix = "✅ " if index == int(source_index) else ""
            source_row.append(
                Button.inline(
                    f"{prefix}{entry[:20]}",
                    f"ui:pick:{index}:0".encode(),
                )
            )
        if source_row:
            rows.append(source_row)
        window_label = "今天 ✅" if window == "today" else "今天"
        rows.append(
            [
                Button.inline(
                    window_label,
                    f"ui:sd:{int(source_index)}:{int(page)}:t".encode(),
                ),
                Button.inline(
                    "近2天" + (" ✅" if window == "2d" else ""),
                    f"ui:sd:{int(source_index)}:{int(page)}:d".encode(),
                ),
            ]
        )
        filter_label = "只看视频 ✅" if video_only else "只看视频"
        rows.append(
            [
                Button.inline(
                    filter_label,
                    f"ui:sf:{int(source_index)}:{int(page)}".encode(),
                ),
                Button.inline("🔄 刷新", f"ui:pr:{int(source_index)}:{int(page)}".encode()),
            ]
        )
        previews = self._preview_count(owner_id)
        if previews:
            rows.append(
                [
                    Button.inline(
                        f"🧹 清理预览 ({previews})",
                        f"ui:pc:{int(source_index)}:{int(page)}".encode(),
                    )
                ]
            )
        ads_label = "🚫 广告过滤 ✅" if ads_hidden else "🚫 广告过滤 ❌"
        ad_row = [
            Button.inline(ads_label, f"ui:sa:{int(source_index)}:{int(page)}".encode())
        ]
        if hidden:
            ad_row.append(
                Button.inline(
                    f"👀 查看被隐藏 ({len(hidden)})",
                    f"ui:sh:{int(source_index)}:0".encode(),
                )
            )
        if submitted:
            ad_row.append(
                Button.inline(
                    f"✅ 已提交 ({len(submitted)})",
                    f"ui:ps:{int(source_index)}:0".encode(),
                )
            )
        rows.append(ad_row)
        rows.append([Button.inline("⬅️ 来源设置", b"ui:source"), Button.inline("🏠 首页", b"ui:home")])
        return "\n".join(lines), rows, summaries

    async def _show_pick_callback(
        self,
        event,
        owner_id: int,
        source_index: int,
        page: int,
        *,
        with_grid: bool = True,
        refresh: bool = False,
    ) -> None:
        text, rows, summaries = await self._pick_render(
            owner_id, source_index, page, refresh=refresh
        )
        await self._render_pick_page(event, owner_id, text, rows)
        if with_grid:
            self._start_page_grid(
                owner_id,
                int(event.chat_id),
                source_index,
                page,
                [summary.message_id for summary in summaries],
            )

    async def _render_pick_page(self, event, owner_id: int, text: str, rows) -> None:
        """Draw the pick page into the tracked list message when possible.

        Preview messages carry their own "back to list" button, so editing the
        event message would turn that preview into the list; editing the tracked
        list message keeps every preview intact.
        """

        chat_id = int(event.chat_id)
        current = getattr(event, "message_id", None)
        tracked = self._pick_list_messages().get(int(owner_id))
        if tracked is not None and (current is None or int(tracked) != int(current)):
            if await self._edit_text_buttons(chat_id, int(tracked), text, rows):
                await self._safe_answer(event)
                return
            self._pick_list_messages().pop(int(owner_id), None)
        await self._edit_page(event, text, rows)
        if current is not None:
            self._pick_list_messages()[int(owner_id)] = int(current)

    # ------------------------------------------------------------ grid build
    def _start_page_grid(
        self,
        owner_id: int,
        chat_id: int,
        source_index: int,
        page: int,
        message_ids: list[int],
    ) -> None:
        service = self._pick_preview_service()
        if service is None or not message_ids:
            return
        task = asyncio.ensure_future(
            self._build_page_grid(owner_id, chat_id, source_index, page, list(message_ids))
        )
        tasks = getattr(self, "_tasks", None)
        if tasks is not None:
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _build_page_grid(
        self,
        owner_id: int,
        chat_id: int,
        source_index: int,
        page: int,
        message_ids: list[int],
    ) -> None:
        service = self._pick_preview_service()
        if service is None:
            return
        total = len(message_ids)
        progress = await self._send_text(chat_id, f"⏳ 正在生成第 {page + 1} 页缩略图 0/{total} …")
        progress_id = getattr(progress, "id", None)
        last = 0

        async def report(done: int, count: int) -> None:
            nonlocal last
            if progress_id is None or done == last:
                return
            last = done
            await self._edit_text(
                chat_id,
                int(progress_id),
                f"⏳ 正在生成第 {page + 1} 页缩略图 {done}/{count} …",
            )

        try:
            preview = await service.build_page(source_index, message_ids, progress=report)
        except Exception:  # noqa: BLE001 - previews must never break picking
            preview = None
        if preview is None or preview.image is None:
            available = 0 if preview is None else preview.fetched
            if progress_id is not None:
                if available:
                    warning = (
                        f"⚠️ 预览图生成失败（已取到 {available}/{total}）；"
                        "可点每行 👁 单独看。"
                    )
                else:
                    warning = f"⚠️ 没有取到缩略图（0/{total}）；可点每行 👁 单独看。"
                await self._edit_text(chat_id, int(progress_id), warning)
            return
        previous = self._pick_grid_messages().pop(int(owner_id), None)
        if previous is not None:
            await self._delete_message(chat_id, previous)
        if progress_id is not None:
            await self._delete_message(chat_id, int(progress_id))
        caption = f"第 {page + 1} 页缩略图 · 位置对应列表 1–{total}"
        if getattr(preview, "fetched", total) < total:
            caption += f"（{preview.fetched}/{preview.total} 张有预览）"
        message = await self._send_photo(
            chat_id,
            preview.image,
            caption,
            [
                [
                    Button.inline(
                        "🔄 整页刷新",
                        f"ui:pick:{int(source_index)}:{int(page)}".encode(),
                    ),
                    Button.inline("⬅️ 返回列表", b"ui:source"),
                ]
            ],
        )
        service.release(preview.token)
        if message is not None and getattr(message, "id", None) is not None:
            self._pick_grid_messages()[int(owner_id)] = int(message.id)
