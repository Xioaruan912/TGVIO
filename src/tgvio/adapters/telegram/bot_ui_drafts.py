from __future__ import annotations

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403


_PAGE_SIZE = 5


class BotUIDraftsMixin:
    async def _render_drafts(self, owner_id: int, *, page: int = 0) -> tuple[str, list]:
        drafts = await self._repository.list_drafts(int(owner_id), limit=20)
        total = len(drafts)
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(int(page), total_pages - 1))
        window = drafts[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
        lines = [
            f"📝 **我的草稿** · 第 `{page + 1}/{total_pages}` 页 · 共 `{total}`",
            "──────────",
        ]
        if not drafts:
            lines.append("还没有保存的草稿。收集中点“保存草稿”即可稍后继续。")
        rows: list[list] = []
        for index, draft in enumerate(window, start=1):
            entries = await self._repository.list_draft_entries(draft.session_id)
            visible = [
                item
                for item in entries
                if item.entry.kind.value == "media" and not item.excluded
            ]
            text_entries = [item for item in entries if item.entry.kind.value == "text"]
            state_label = {
                "collecting": "收集中",
                "preview": "待确认",
                "saved": "已保存",
            }.get(draft.state.value, draft.state.value)
            lines.append(
                f"{index}. {'✏️' if draft.active else '💾'} {state_label} · "
                f"`{len(visible)}` 媒体 · `{len(text_entries)}` 文字"
            )
            rows.append(
                [
                    Button.inline(
                        f"{index} 继续编辑",
                        f"ui:draft-open:{draft.session_id}".encode("utf-8"),
                    ),
                    Button.inline(
                        f"{index} 删除",
                        f"ui:draft-del:{draft.session_id}".encode("utf-8"),
                    ),
                ]
            )
        nav: list = []
        if page > 0:
            nav.append(Button.inline("⬅️", f"ui:drafts:{page - 1}".encode()))
        nav.append(Button.inline("🔄", f"ui:drafts:{page}".encode()))
        if page + 1 < total_pages:
            nav.append(Button.inline("➡️", f"ui:drafts:{page + 1}".encode()))
        if nav:
            rows.append(nav)
        rows.append(
            [Button.inline("🏠 首页", b"ui:home"), Button.inline("📋 我的任务", b"ui:jobs")]
        )
        return "\n".join(lines), rows

    async def _show_drafts_callback(self, event, owner_id: int, page_token: str) -> None:
        try:
            page = max(0, int(page_token))
        except (TypeError, ValueError):
            page = 0
        text, rows = await self._render_drafts(owner_id, page=page)
        await self._edit_page(event, text, rows)

    async def _draft_open_callback(self, event, owner_id: int, session_id: str) -> None:
        draft = await self._repository.get_draft(session_id)
        if draft is None or int(draft.owner_id) != int(owner_id):
            await self._safe_answer(event, "草稿已失效", alert=True)
            return
        activated = await self._repository.activate_draft(session_id)
        if activated is None:
            await self._safe_answer(event, "草稿已失效", alert=True)
            return
        await self._safe_answer(event, "已打开草稿；继续发送媒体或点结束发布")
        try:
            await self._client.send_message(
                int(event.chat_id),
                "✏️ **已打开草稿**\n\n继续发送图片/视频或文字补充内容，完成后点消息里的“预览与整理”。",
                buttons=self._reply_keyboard(),
                parse_mode="md",
            )
        except Exception:
            pass

    async def _draft_delete_callback(self, event, owner_id: int, session_id: str) -> None:
        draft = await self._repository.get_draft(session_id)
        if draft is None or int(draft.owner_id) != int(owner_id):
            await self._safe_answer(event, "草稿已失效", alert=True)
            return
        session = await self._repository.get_collection(session_id)
        if session is not None and session.state.value == "open":
            await self._repository.cancel_collection(session_id)
        else:
            await self._repository.mark_draft_discarded(session_id)
        await self._safe_answer(event, "草稿已删除")
        text, rows = await self._render_drafts(owner_id, page=0)
        await self._edit_page(event, text, rows)
