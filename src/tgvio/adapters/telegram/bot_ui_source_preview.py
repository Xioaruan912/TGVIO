"""Row previews for the source picker.

A preview is one photo message per picked row that stays in the chat so several
rows can be compared side by side (the owner uses them to see which rows belong
to the same album). Each preview carries its own selection button, so picking can
be done straight from the thumbnails. Previews only disappear after a successful
publish/grab, when the owner taps the manual cleanup button, when the cap is
exceeded or after the TTL expires.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403

_PREVIEW_TTL_SECONDS = 600
_MAX_PREVIEW_MESSAGES = 10


class BotUISourcePreviewMixin:
    # ------------------------------------------------------------ bookkeeping
    def _pick_preview_messages(self) -> dict[int, list[int]]:
        state = getattr(self, "_pick_preview_msgs", None)
        if state is None:
            state = {}
            self._pick_preview_msgs = state
        return state

    def _pick_preview_rows(self) -> dict[tuple[int, int], tuple[int, int, int, int]]:
        """``(owner, photo_id) -> (source_index, page, position, source_message_id)``."""

        state = getattr(self, "_pick_preview_rows_map", None)
        if state is None:
            state = {}
            self._pick_preview_rows_map = state
        return state

    def _preview_count(self, owner_id: int) -> int:
        return len(self._pick_preview_messages().get(int(owner_id), []))

    def _is_preview_message(self, owner_id: int, event) -> bool:
        """Whether a callback came from one of the accumulated preview photos."""

        return self._preview_message_id(owner_id, event) is not None

    def _preview_message_id(self, owner_id: int, event) -> int | None:
        """The preview photo id behind a callback, when it is one of ours."""

        message_id = getattr(event, "message_id", None)
        if message_id is None:
            return None
        key = (int(owner_id), int(message_id))
        return int(message_id) if key in self._pick_preview_rows() else None

    async def _track_preview(
        self,
        owner_id: int,
        chat_id: int,
        message_id: int,
        *,
        source_index: int = 0,
        page: int = 0,
        position: int = 0,
        source_message_id: int = 0,
    ) -> None:
        """Remember one preview message so it stays until the owner acts."""

        tracked = self._pick_preview_messages().setdefault(int(owner_id), [])
        tracked.append(int(message_id))
        self._pick_preview_rows()[(int(owner_id), int(message_id))] = (
            int(source_index),
            int(page),
            int(position),
            int(source_message_id),
        )
        while len(tracked) > _MAX_PREVIEW_MESSAGES:
            oldest = tracked.pop(0)
            self._pick_preview_rows().pop((int(owner_id), oldest), None)
            await self._delete_message(chat_id, oldest)
        self._schedule_preview_expiry(owner_id, chat_id, int(message_id))

    async def _clear_pick_previews(self, owner_id: int, chat_id: int) -> int:
        """Destroy every accumulated preview message; returns how many there were."""

        tracked = self._pick_preview_messages().pop(int(owner_id), [])
        rows = self._pick_preview_rows()
        for message_id in tracked:
            rows.pop((int(owner_id), int(message_id)), None)
            await self._delete_message(chat_id, int(message_id))
        return len(tracked)

    # ---------------------------------------------------------------- buttons
    def _preview_selected(self, owner_id: int, source_index: int, message_id: int) -> bool:
        key = self._selection_key(source_index, message_id)
        return key in self._selection_for(owner_id)["meta"]

    def _preview_buttons(
        self,
        owner_id: int,
        source_index: int,
        message_id: int,
        page: int,
        position: int,
    ) -> list[list]:
        selected = self._preview_selected(owner_id, source_index, message_id)
        mark = f"✅ 已选 {position}" if selected else f"☑️ 选择 {position}"
        return [
            [
                Button.inline(
                    mark,
                    f"ui:pk:{int(source_index)}:{int(message_id)}:{int(page)}".encode(),
                ),
                Button.inline(
                    "📥 抓取",
                    f"ui:sg:{int(source_index)}:{int(message_id)}:{int(page)}".encode(),
                ),
            ],
            [
                Button.inline(
                    "🗑 移除",
                    f"ui:srm:{int(source_index)}:{int(page)}:{int(message_id)}".encode(),
                ),
                Button.inline(
                    "⬅️ 返回列表",
                    f"ui:pick:{int(source_index)}:{int(page)}".encode(),
                ),
            ],
        ]

    def _preview_caption(
        self,
        owner_id: int,
        source_index: int,
        summary,
        position: int,
        total: int,
    ) -> str:
        label = self._summary_line(summary) if summary is not None else "整组预览"
        heading = f"👁 第 {int(position)} 项 · {label}" if int(position) > 0 else f"👁 {label}"
        lines = [heading, f"位置对应组内第 1–{total} 项"]
        if summary is not None and self._preview_selected(
            owner_id, source_index, summary.message_id
        ):
            totals = self._selection_summary(owner_id)
            lines.append(f"✅ 已加入合并发布（共 {totals['rows']} 组）")
        return "\n".join(lines)

    async def _refresh_preview(self, owner_id: int, chat_id: int, message_id: int) -> None:
        """Re-draw one preview so its selection button matches the selection."""

        row = self._pick_preview_rows().get((int(owner_id), int(message_id)))
        if row is None:
            return
        source_index, page, position, source_message_id = row
        summary = self._pick_cache().get(
            (int(owner_id), int(source_index), int(page)), {}
        ).get(int(source_message_id))
        caption = self._preview_caption(
            owner_id, source_index, summary, position, 1
        )
        buttons = self._preview_buttons(
            owner_id, source_index, source_message_id, page, position
        )
        with suppress(Exception):
            await self._client.edit_message(
                int(chat_id), int(message_id), caption, buttons=buttons
            )

    # ------------------------------------------------------------------ build
    async def _preview_single(
        self,
        event,
        owner_id: int,
        source_index: int,
        message_id: int,
        page: int,
        position: int = 0,
    ) -> None:
        service = self._pick_preview_service()
        chat_id = int(event.chat_id)
        if service is None:
            await self._safe_answer(event, "预览不可用", alert=True)
            return
        await self._safe_answer(event, "正在获取预览…")
        summary = self._pick_cache().get((int(owner_id), int(source_index), int(page)), {}).get(
            int(message_id)
        )
        coordinator = self._source_coordinator()
        group_ids: list[int] = []
        if coordinator is not None:
            with suppress(Exception):
                group_ids = await coordinator.group_message_ids(source_index, int(message_id))
        if not group_ids:
            group_ids = [int(message_id)]
        total = len(group_ids)
        progress = await self._send_text(
            chat_id, f"⏳ 正在生成整组缩略图 0/{total} …"
        )
        progress_id = getattr(progress, "id", None)
        last = 0

        async def report(done: int, count: int) -> None:
            nonlocal last
            if progress_id is None or done == last:
                return
            last = done
            await self._edit_text(
                chat_id, int(progress_id), f"⏳ 正在生成整组缩略图 {done}/{count} …"
            )

        try:
            preview = await service.build_page(source_index, group_ids, progress=report)
        except Exception:  # noqa: BLE001
            preview = None
        if preview is None or preview.image is None:
            if progress_id is not None:
                available = 0 if preview is None else preview.fetched
                await self._edit_text(
                    chat_id,
                    int(progress_id),
                    f"⚠️ 整组预览生成失败（{available}/{total}）；可直接点 📥 抓取。",
                )
            return
        if progress_id is not None:
            await self._delete_message(chat_id, int(progress_id))
        message = await self._send_photo(
            chat_id,
            preview.image,
            self._preview_caption(owner_id, source_index, summary, position, total),
            self._preview_buttons(
                owner_id, source_index, message_id, page, position
            ),
        )
        service.release(preview.token)
        if message is not None and getattr(message, "id", None) is not None:
            await self._track_preview(
                owner_id,
                chat_id,
                int(message.id),
                source_index=source_index,
                page=page,
                position=position,
                source_message_id=int(message_id),
            )

    # ----------------------------------------------------------------- expiry
    def _schedule_preview_expiry(self, owner_id: int, chat_id: int, message_id: int) -> None:
        task = asyncio.ensure_future(
            self._expire_preview(owner_id, chat_id, message_id)
        )
        tasks = getattr(self, "_tasks", None)
        if tasks is not None:
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _expire_preview(self, owner_id: int, chat_id: int, message_id: int) -> None:
        try:
            await asyncio.sleep(_PREVIEW_TTL_SECONDS)
        except asyncio.CancelledError:
            return
        tracked = self._pick_preview_messages().get(int(owner_id), [])
        if int(message_id) not in tracked:
            return
        tracked.remove(int(message_id))
        self._pick_preview_rows().pop((int(owner_id), int(message_id)), None)
        await self._delete_message(chat_id, int(message_id))
