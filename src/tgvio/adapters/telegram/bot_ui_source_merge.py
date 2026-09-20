"""Merged multi-row publishing for the source picker.

Rows are toggled into an owner-scoped selection; publishing the selection reads
every chosen group once and submits them as a single job so the albums come out
merged (photos and videos together, one album per ten items).
"""

from __future__ import annotations

from contextlib import suppress

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403

_PREVIEW_TILES = 10


class BotUISourceMergeMixin:
    def _pick_selection(self) -> dict[int, dict]:
        """Owner-scoped merged selection: order plus per-row metadata."""

        state = getattr(self, "_pick_selections", None)
        if state is None:
            state = {}
            self._pick_selections = state
        return state

    def _selection_for(self, owner_id: int) -> dict:
        return self._pick_selection().setdefault(
            int(owner_id), {"order": [], "meta": {}}
        )

    @staticmethod
    def _selection_key(source_index: int, message_id: int) -> tuple[int, int]:
        return (int(source_index), int(message_id))

    def _selection_summary(self, owner_id: int) -> dict:
        """Aggregate the current selection for headers and the confirm card."""

        selection = self._selection_for(owner_id)
        meta = selection["meta"]
        totals = {
            "rows": len(selection["order"]),
            "items": 0,
            "videos": 0,
            "photos": 0,
            "bytes": 0,
        }
        for key in selection["order"]:
            entry = meta.get(key)
            if not entry:
                continue
            totals["items"] += int(entry.get("items", 0))
            totals["videos"] += int(entry.get("videos", 0))
            totals["photos"] += int(entry.get("photos", 0))
            totals["bytes"] += int(entry.get("bytes", 0))
        return totals

    def _toggle_selection(self, owner_id: int, source_index: int, summary) -> None:
        selection = self._selection_for(owner_id)
        key = self._selection_key(source_index, summary.message_id)
        if key in selection["meta"]:
            selection["meta"].pop(key, None)
            selection["order"] = [entry for entry in selection["order"] if entry != key]
            return
        selection["order"].append(key)
        selection["meta"][key] = {
            "items": int(summary.item_count),
            "videos": int(summary.video_count),
            "photos": int(summary.photo_count),
            "bytes": int(summary.size_bytes),
        }

    def _selection_composition(self, totals: dict) -> str:
        parts: list[str] = []
        if totals["videos"]:
            parts.append(f"🎬{totals['videos']}")
        if totals["photos"]:
            parts.append(f"🖼{totals['photos']}")
        return " ".join(parts) or "媒体"

    def _selection_lines(self, owner_id: int) -> list[str]:
        totals = self._selection_summary(owner_id)
        if not totals["rows"]:
            return ["还没有选择任何一行。"]
        return [
            f"共 {totals['rows']} 组 · {totals['items']} 项 · "
            f"{self._selection_composition(totals)} · {self._human_bytes(totals['bytes'])}",
            "合并后按 10 项一个相册发在同一个频道帖的评论区。",
        ]

    def _merge_limit(self) -> int:
        settings = getattr(self, "_settings", None)
        return int(getattr(settings, "source_merge_max_items", 100) or 100)

    async def _merge_confirm_card(
        self,
        event,
        owner_id: int,
        source_index: int,
        page: int,
    ) -> None:
        totals = self._selection_summary(owner_id)
        if not totals["rows"]:
            await self._safe_answer(event, "还没有选择", alert=True)
            return
        limit = self._merge_limit()
        lines = ["合并发布", "──────────", *self._selection_lines(owner_id)]
        if totals["items"] > limit:
            lines.append(
                f"⚠️ 超过一次上限 {limit} 项；请分批，或调大 `TGVIO_MERGE_MAX_ITEMS`。"
            )
        lines.append("──────────")
        rows = [
            [
                Button.inline("✅ 发布", f"ui:sm:{int(source_index)}:{int(page)}".encode()),
                Button.inline("👁 预览", f"ui:spm:{int(source_index)}:{int(page)}".encode()),
            ],
            [Button.inline("❌ 取消", f"ui:sx:{int(source_index)}:{int(page)}".encode())],
        ]
        await self._edit_page(event, "\n".join(lines), rows)

    async def _merged_preview(
        self,
        event,
        owner_id: int,
        source_index: int,
        page: int,
    ) -> None:
        service = self._pick_preview_service()
        coordinator = self._source_coordinator()
        selection = self._selection_for(owner_id)
        if service is None or coordinator is None or not selection["order"]:
            await self._safe_answer(event, "没有可预览的选择", alert=True)
            return
        await self._safe_answer(event, "正在准备预览…")
        chat_id = int(event.chat_id)
        ids: list[int] = []
        for key_source, key_message in selection["order"]:
            if len(ids) >= _PREVIEW_TILES:
                break
            with suppress(Exception):
                ids.extend(await coordinator.group_message_ids(key_source, key_message))
        ids = list(dict.fromkeys(ids))[:_PREVIEW_TILES]
        if not ids:
            await self._send_text(chat_id, "⚠️ 这些行都读不到，无法预览。")
            return
        total = len(ids)
        progress = await self._send_text(chat_id, f"⏳ 正在生成合并预览 0/{total} …")
        progress_id = getattr(progress, "id", None)
        last = 0

        async def report(done: int, count: int) -> None:
            nonlocal last
            if progress_id is None or done == last:
                return
            last = done
            await self._edit_text(
                chat_id, int(progress_id), f"⏳ 正在生成合并预览 {done}/{count} …"
            )

        try:
            preview = await service.build_page(source_index, ids, progress=report)
        except Exception:  # noqa: BLE001 - previews must never break picking
            preview = None
        if preview is None or preview.image is None:
            if progress_id is not None:
                await self._edit_text(chat_id, int(progress_id), "⚠️ 合并预览生成失败。")
            return
        if progress_id is not None:
            await self._delete_message(chat_id, int(progress_id))
        totals = self._selection_summary(owner_id)
        caption = (
            f"👁 合并预览（{totals['rows']} 组 · 前 {total} 项）\n"
            "位置按选择顺序对应各组条目。"
        )
        message = await self._send_photo(
            chat_id,
            preview.image,
            caption,
            [
                [
                    Button.inline("✅ 发布", f"ui:sm:{int(source_index)}:{int(page)}".encode()),
                    Button.inline(
                        "⬅️ 返回列表", f"ui:pick:{int(source_index)}:{int(page)}".encode()
                    ),
                ]
            ],
        )
        service.release(preview.token)
        if message is not None and getattr(message, "id", None) is not None:
            await self._track_preview(owner_id, chat_id, int(message.id))

    async def _publish_merged(
        self,
        event,
        owner_id: int,
        source_index: int,
        page: int,
    ) -> None:
        coordinator = self._source_coordinator()
        selection = self._selection_for(owner_id)
        chat_id = int(event.chat_id)
        if coordinator is None or not selection["order"]:
            await self._safe_answer(event, "还没有选择", alert=True)
            return
        totals = self._selection_summary(owner_id)
        if totals["items"] > self._merge_limit():
            await self._safe_answer(event, "超过一次上限，请分批", alert=True)
            return
        await self._safe_answer(event, "已提交，正在合并…")
        selections = list(selection["order"])
        self._pick_selection().pop(int(owner_id), None)
        progress = await self._send_text(chat_id, f"⏳ 正在合并读取 {len(selections)} 组 …")
        progress_id = getattr(progress, "id", None)
        try:
            count, label, failed, accepted, skipped = await coordinator.grab_selection(
                selections
            )
        except Exception:  # noqa: BLE001 - user-facing miss
            if progress_id is not None:
                await self._delete_message(chat_id, int(progress_id))
            await self._send_text(chat_id, "⚠️ 合并抓取失败，请稍后重试。")
            await self._show_pick_callback(
                event, owner_id, source_index, page, with_grid=False
            )
            return
        if progress_id is not None:
            await self._delete_message(chat_id, int(progress_id))
        if not count:
            detail = f"（{failed} 组读取失败）" if failed else ""
            await self._send_text(chat_id, f"⚠️ 没有读到可用媒体{detail}；已刷新列表。")
        else:
            lines = [f"✅ 已合并提交 `{accepted}` 项（来源：`{label}`），正在下载与发布。"]
            if skipped:
                lines.append(f"跳过 `{skipped}` 项：之前已经发布过。")
            if failed:
                lines.append(f"跳过 `{failed}` 组：读取失败。")
            await self._send_text(chat_id, "\n".join(lines))
        await self._show_pick_callback(
            event, owner_id, source_index, page, with_grid=False
        )
