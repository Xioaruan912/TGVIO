from __future__ import annotations

import asyncio
import logging

from tgvio.adapters.telegram.intake_runtime_support import *  # noqa: F401,F403
from tgvio.application.collection_editing import (
    DraftRevisionConflict,
    DraftUnavailableError,
)
from tgvio.domain.collection_editing import DraftState
from tgvio.application.operation_tokens import OperationTokenInvalidError
from tgvio.domain.intake import CollectionEntryKind


_PAGE_SIZE = 5


class IntakeEditMixin:
    """Revision-based collection editing panel, draft list and frozen confirm token."""

    def _editing_enabled(self) -> bool:
        if getattr(self, "_editing", None) is None:
            return False
        if not bool(getattr(self._settings, "collection_editing_enabled", True)):
            return False
        flags = getattr(self, "_flags", None)
        if flags is not None:
            try:
                return flags.bool("collection_editing_enabled", True)
            except Exception:
                return True
        return True

    async def _editing_service(self):
        return getattr(self, "_editing", None)

    # ------------------------------------------------------------- callbacks
    async def _handle_edit_callback(self, event, action: str, owner_id: int) -> bool:
        """Return True when the callback was handled by the editing panel."""
        if action.startswith("intake:ed:"):
            await self._on_edit_panel(event, action, owner_id)
            return True
        if action.startswith("intake:df:"):
            await self._on_draft_list(event, action, owner_id)
            return True
        if action.startswith("intake:sg:"):
            await self._on_suggestion(event, action, owner_id)
            return True
        if action.startswith("intake:pv:"):
            await self._on_preview_request(event, action, owner_id)
            return True
        if action.startswith("intake:cc:"):
            await self._on_edit_confirm(event, action, owner_id)
            return True
        return False

    async def _parse_edit_action(self, event, action: str):
        parts = action.split(":")
        # intake : ed : <session> : <rev> : <op> [ : <entry_id> ]
        if len(parts) < 5:
            await self._safe_answer(event, "编辑操作已过期", alert=True)
            return None
        session_id = parts[2]
        try:
            revision = int(parts[3])
        except (TypeError, ValueError):
            await self._safe_answer(event, "编辑操作已过期", alert=True)
            return None
        operation = parts[4]
        entry_id = None
        if len(parts) >= 6:
            try:
                entry_id = int(parts[5])
            except (TypeError, ValueError):
                entry_id = None
        return session_id, revision, operation, entry_id

    async def _on_edit_panel(self, event, action: str, owner_id: int) -> None:
        editing = await self._editing_service()
        parsed = await self._parse_edit_action(event, action)
        if parsed is None or editing is None:
            return
        session_id, revision, operation, entry_id = parsed
        draft = await editing.draft(session_id)
        if draft is None or int(draft.owner_id) != int(owner_id):
            await self._safe_answer(event, "草稿已失效", alert=True)
            return
        try:
            if operation == "p":
                await self._safe_answer(event, "已刷新")
                await self._render_edit_panel(event.chat_id, owner_id, session_id, draft, page=entry_id or 0)
                return
            if int(draft.revision) != int(revision):
                await self._safe_answer(event, "内容已更新，请刷新", alert=True)
                await self._render_edit_panel(event.chat_id, owner_id, session_id, draft)
                return
            if operation == "x" and entry_id is not None:
                draft = await editing.toggle_exclude(
                    owner_id=owner_id,
                    session_id=session_id,
                    entry_id=entry_id,
                    expected_revision=revision,
                )
            elif operation in {"u", "n"} and entry_id is not None:
                draft = await editing.move(
                    owner_id=owner_id,
                    session_id=session_id,
                    entry_id=entry_id,
                    direction=1 if operation == "u" else -1,
                    expected_revision=revision,
                )
            elif operation == "c" and entry_id is not None:
                draft = await editing.select_cover(
                    owner_id=owner_id,
                    session_id=session_id,
                    entry_id=entry_id,
                    expected_revision=revision,
                )
            elif operation == "z":
                draft = await editing.clear_cover(
                    owner_id=owner_id, session_id=session_id, expected_revision=revision
                )
            elif operation == "T":
                draft = await editing.clear_caption(
                    owner_id=owner_id, session_id=session_id, expected_revision=revision
                )
            elif operation == "s":
                draft = await editing.save(
                    owner_id=owner_id, session_id=session_id, expected_revision=revision
                )
                await self._safe_answer(event, "草稿已保存")
                await self._render_draft_list(event.chat_id, owner_id)
                return
            elif operation == "o":
                await self._safe_answer(event, "编辑合集")
                await self._render_edit_panel(event.chat_id, owner_id, session_id, draft)
                return
            elif operation == "f":
                await self._safe_answer(event, "已刷新")
                await self._render_edit_panel(event.chat_id, owner_id, session_id, draft)
                return
            elif operation == "t":
                interaction = await editing.begin_caption(
                    owner_id=owner_id,
                    chat_id=int(event.chat_id),
                    session_id=session_id,
                    expected_revision=revision,
                )
                await self._safe_answer(event, "请发送新文案")
                await self._safe_edit(
                    int(event.chat_id),
                    await self._panel_message_id(session_id),
                    "📝 **修改文案**\n\n请在 3 分钟内直接发送新的文案文本（可多行）。\n发送后会替换合集封面文案；也可点“清除文案”恢复自动汇总。",
                    buttons=[
                        [
                            Button.inline(
                                "🧹 清除文案",
                                f"intake:ed:{session_id}:{revision}:T".encode("utf-8"),
                            )
                        ],
                        [
                            Button.inline(
                                "↩️ 返回编辑",
                                f"intake:ed:{session_id}:{revision}:f".encode("utf-8"),
                            )
                        ],
                    ],
                )
                return
            elif operation == "k":
                await self._safe_answer(event, "准备确认发布")
                await self._issue_and_show_confirm(event.chat_id, owner_id, session_id, draft)
                return
            else:
                await self._safe_answer(event, "编辑操作已过期", alert=True)
                return
        except DraftRevisionConflict:
            await self._safe_answer(event, "内容已更新，请刷新", alert=True)
            draft = await editing.draft(session_id)
            if draft is not None:
                await self._render_edit_panel(event.chat_id, owner_id, session_id, draft)
            return
        except (DraftUnavailableError, OperationTokenInvalidError):
            await self._safe_answer(event, "草稿已失效", alert=True)
            return
        await self._render_edit_panel(event.chat_id, owner_id, session_id, draft)

    async def _panel_message_id(self, session_id: str) -> int:
        session = await self._repository.get_collection(session_id)
        if session is None or session.status_message_id is None:
            raise DraftUnavailableError("collection status message is unavailable")
        return int(session.status_message_id)

    async def _render_edit_panel(
        self,
        chat_id: int,
        owner_id: int,
        session_id: str,
        draft,
        *,
        page: int = 0,
    ) -> None:
        editing = await self._editing_service()
        if editing is None:
            return
        entries = [
            item
            for item in await editing.entries(session_id)
            if item.entry.kind == CollectionEntryKind.MEDIA
        ]
        total_pages = max(1, (len(entries) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(int(page), total_pages - 1))
        start = page * _PAGE_SIZE
        window = entries[start : start + _PAGE_SIZE]
        cover_id = draft.cover_entry_id
        lines = [
            f"🧩 **合集编辑** · rev `{draft.revision}` · 第 `{page + 1}/{total_pages}` 页",
            "──────────",
        ]
        if not entries:
            lines.append("还没有媒体。")
        for offset, item in enumerate(window, start=1):
            entry = item.entry
            icon = "🖼" if entry.kind != CollectionEntryKind.MEDIA else (
                "🎬" if str(entry.payload.get("kind")) == "video" else "🖼"
            )
            flag = "（已移除）" if item.excluded else ""
            cover = " · ⭐封面" if entry.id is not None and int(entry.id) == int(cover_id or -1) else ""
            name = str(entry.payload.get("name") or entry.payload.get("kind") or "媒体")[:24]
            size = self._human_bytes(int(entry.payload.get("size_bytes", 0) or 0))
            lines.append(f"{offset}. {icon} {name} · {size}{cover}{flag}")
        lines.extend(["──────────", "点行内按钮调整；改动会自动生效并刷新预览。"])
        text = "\n".join(lines)

        encoded_sid = session_id
        rows: list[list] = []
        for offset, item in enumerate(window, start=1):
            if item.entry.id is None:
                continue
            entry_id = int(item.entry.id)
            rows.append(
                [
                    Button.inline("⬆️", f"intake:ed:{encoded_sid}:{draft.revision}:u:{entry_id}".encode()),
                    Button.inline("⬇️", f"intake:ed:{encoded_sid}:{draft.revision}:n:{entry_id}".encode()),
                    Button.inline("🎯", f"intake:ed:{encoded_sid}:{draft.revision}:c:{entry_id}".encode()),
                    Button.inline(
                        "♻️" if item.excluded else "🗑",
                        f"intake:ed:{encoded_sid}:{draft.revision}:x:{entry_id}".encode(),
                    ),
                ]
            )
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(Button.inline("⬅️", f"intake:ed:{encoded_sid}:{draft.revision}:p:{page - 1}".encode()))
            nav.append(Button.inline("🔄", f"intake:ed:{encoded_sid}:{draft.revision}:p:{page}".encode()))
            if page + 1 < total_pages:
                nav.append(Button.inline("➡️", f"intake:ed:{encoded_sid}:{draft.revision}:p:{page + 1}".encode()))
            rows.append(nav)
        rows.append(
            [
                Button.inline("📝 改文案", f"intake:ed:{encoded_sid}:{draft.revision}:t".encode()),
                Button.inline("🧹 清封面", f"intake:ed:{encoded_sid}:{draft.revision}:z".encode()),
            ]
        )
        rows.append(
            [
                Button.inline("🧠 整理建议", f"intake:sg:{encoded_sid}:{draft.revision}:a".encode()),
                Button.inline("🖼 效果预览", f"intake:pv:{encoded_sid}:{draft.revision}".encode()),
            ]
        )
        rows.append(
            [
                Button.inline("💾 保存草稿", f"intake:ed:{encoded_sid}:{draft.revision}:s".encode()),
                Button.inline("🧹 清封面", f"intake:ed:{encoded_sid}:{draft.revision}:z".encode()),
            ]
        )
        rows.append(
            [
                Button.inline("✅ 确认发布", f"intake:ed:{encoded_sid}:{draft.revision}:k".encode()),
                Button.inline("❌ 放弃合集", f"intake:abandon:{encoded_sid}".encode()),
            ]
        )
        try:
            await self._safe_edit(
                int(chat_id),
                await self._panel_message_id(session_id),
                text,
                buttons=rows,
            )
        except DraftUnavailableError:
            await self._safe_send(int(chat_id), text, buttons=rows)

    async def _on_suggestion(self, event, action: str, owner_id: int) -> None:
        editing = await self._editing_service()
        if editing is None:
            await self._safe_answer(event, "编辑功能不可用", alert=True)
            return
        parts = action.split(":")
        if len(parts) < 5:
            await self._safe_answer(event, "操作已过期", alert=True)
            return
        session_id = parts[2]
        try:
            revision = int(parts[3])
        except (TypeError, ValueError):
            await self._safe_answer(event, "操作已过期", alert=True)
            return
        operation = parts[4]
        entry_id = int(parts[5]) if len(parts) >= 6 and parts[5].isdigit() else None
        draft = await editing.draft(session_id)
        if draft is None or int(draft.owner_id) != int(owner_id):
            await self._safe_answer(event, "草稿已失效", alert=True)
            return
        from tgvio.application.suggestions import SuggestionService

        service = SuggestionService(self._repository, editing)
        try:
            if operation == "a":
                await self._safe_answer(event, "已生成建议")
                await self._render_suggestions(event.chat_id, owner_id, session_id, draft)
                return
            if int(draft.revision) != int(revision):
                await self._safe_answer(event, "内容已更新，请刷新", alert=True)
                await self._render_edit_panel(event.chat_id, owner_id, session_id, draft)
                return
            if operation == "c" and entry_id is not None:
                draft = await service.apply_cover(
                    owner_id=owner_id,
                    session_id=session_id,
                    entry_id=entry_id,
                    expected_revision=revision,
                )
                await self._safe_answer(event, "已应用封面建议")
            elif operation == "o":
                draft = await service.apply_order(
                    owner_id=owner_id, session_id=session_id, expected_revision=revision
                )
                await self._safe_answer(event, "已应用排序建议")
            elif operation == "u":
                draft = await service.undo(
                    owner_id=owner_id, session_id=session_id, expected_revision=revision
                )
                await self._safe_answer(event, "已撤回调整")
            else:
                await self._safe_answer(event, "操作已过期", alert=True)
                return
        except DraftRevisionConflict:
            await self._safe_answer(event, "内容已更新，请刷新", alert=True)
            draft = await editing.draft(session_id)
            await self._render_edit_panel(event.chat_id, owner_id, session_id, draft, page=0)
            return
        except DraftUnavailableError:
            await self._safe_answer(event, "没有可撤回的调整", alert=True)
            return
        await self._render_edit_panel(event.chat_id, owner_id, session_id, draft)

    async def _on_preview_request(self, event, action: str, owner_id: int) -> None:
        previews = getattr(self, "_previews", None)
        if previews is None:
            await self._safe_answer(event, "效果预览未启用", alert=True)
            return
        parts = action.split(":")
        if len(parts) < 4:
            await self._safe_answer(event, "操作已过期", alert=True)
            return
        session_id = parts[2]
        try:
            revision = int(parts[3])
        except (TypeError, ValueError):
            await self._safe_answer(event, "操作已过期", alert=True)
            return
        editing = await self._editing_service()
        draft = await editing.draft(session_id) if editing is not None else None
        if draft is None or int(draft.owner_id) != int(owner_id):
            await self._safe_answer(event, "草稿已失效", alert=True)
            return
        if int(draft.revision) != revision:
            await self._safe_answer(event, "内容已更新，请刷新", alert=True)
            await self._render_edit_panel(event.chat_id, owner_id, session_id, draft)
            return
        await self._safe_answer(event, "正在生成效果预览…")
        asyncio.create_task(
            self._run_preview(owner_id, int(event.chat_id), session_id, revision)
        )

    async def _run_preview(
        self, owner_id: int, chat_id: int, session_id: str, revision: int
    ) -> None:
        previews = getattr(self, "_previews", None)
        if previews is None:
            return
        try:
            result = await previews.preview(
                owner_id=owner_id,
                chat_id=chat_id,
                session_id=session_id,
                expected_revision=revision,
            )
            if result.state.value != "succeeded":
                raise RuntimeError("preview unavailable")
        except Exception:
            try:
                await self._safe_send(
                    chat_id,
                    "🖼 未能生成效果预览；已保留文字预览，可继续编辑或直接确认发布。",
                )
            except Exception:
                pass

    async def _render_suggestions(self, chat_id: int, owner_id: int, session_id: str, draft) -> None:
        editing = await self._editing_service()
        if editing is None:
            return
        from tgvio.application.suggestions import SuggestionService
        from tgvio.domain.suggestion import SuggestionKind

        service = SuggestionService(self._repository, editing)
        suggestions = await service.analyze(session_id)
        lines = [f"🧠 **整理建议** · rev `{draft.revision}`", "──────────"]
        if not suggestions:
            lines.append("暂时没有可应用的建议。")
        rows: list[list] = []
        for index, suggestion in enumerate(suggestions, start=1):
            tag = "疑似" if suggestion.uncertain else "规则"
            lines.append(f"{index}. **{suggestion.title}**（{tag}）：{suggestion.detail}")
            if suggestion.kind == SuggestionKind.COVER and suggestion.entry_ids:
                rows.append(
                    [
                        Button.inline(
                            f"{index} 应用封面",
                            f"intake:sg:{session_id}:{draft.revision}:c:{suggestion.entry_ids[0]}".encode(),
                        )
                    ]
                )
            elif suggestion.kind == SuggestionKind.ORDER:
                rows.append(
                    [
                        Button.inline(
                            f"{index} 应用排序",
                            f"intake:sg:{session_id}:{draft.revision}:o".encode(),
                        )
                    ]
                )
        lines.append("──────────")
        lines.append("建议只在你点击后生效；不会自动删除、排序或改动手选封面。")
        application = await self._repository.get_active_suggestion_application(session_id)
        if application is not None and int(application.revision_applied) == int(draft.revision):
            rows.append(
                [
                    Button.inline(
                        "↩️ 撤回上次调整",
                        f"intake:sg:{session_id}:{draft.revision}:u".encode(),
                    )
                ]
            )
        rows.append(
            [
                Button.inline(
                    "↩️ 返回编辑",
                    f"intake:ed:{session_id}:{draft.revision}:f".encode(),
                )
            ]
        )
        try:
            await self._safe_edit(
                int(chat_id),
                await self._panel_message_id(session_id),
                "\n".join(lines),
                buttons=rows,
            )
        except DraftUnavailableError:
            await self._safe_send(int(chat_id), "\n".join(lines), buttons=rows)

    async def _issue_and_show_confirm(
        self,
        chat_id: int,
        owner_id: int,
        session_id: str,
        draft,
    ) -> None:
        editing = await self._editing_service()
        if editing is None:
            return
        preference = await self._intake.get_user_preference(owner_id)
        style_policy = await self._resolve_style(owner_id)
        try:
            token = await editing.issue_confirm(
                owner_id=owner_id,
                session_id=session_id,
                expected_revision=int(draft.revision),
                spoiler_mode=preference.spoiler_mode,
                style_policy=style_policy,
            )
        except Exception as exc:
            await self._safe_send(chat_id, f"⚠️ 无法生成确认：{type(exc).__name__}")
            return
        frozen = await editing.freeze(
            owner_id=owner_id,
            session_id=session_id,
            expected_revision=int(draft.revision),
            spoiler_mode=preference.spoiler_mode,
            style_policy=style_policy,
        )
        style_label = "沿用我的发布风格" if style_policy else "默认风格"
        text = (
            "✅ **确认发布合集**\n"
            "──────────\n"
            f"媒体：`{len(frozen.media)}` 个\n"
            f"封面：`{'已选择' if frozen.cover_entry_id else '自动'}`\n"
            f"风格：`{style_label}`\n"
            "──────────\n"
            "确认后开始下载并发布；旧预览将失效。"
        )
        rows = [
            [Button.inline("✅ 确认发布", f"intake:cc:{token}".encode("utf-8"))],
            [Button.inline("↩️ 返回编辑", f"intake:ed:{session_id}:{draft.revision}:f".encode("utf-8"))],
        ]
        try:
            await self._safe_edit(int(chat_id), await self._panel_message_id(session_id), text, buttons=rows)
        except DraftUnavailableError:
            await self._safe_send(int(chat_id), text, buttons=rows)

    async def _resolve_style(self, owner_id: int) -> dict[str, object] | None:
        repository = getattr(self, "_repository", None)
        if repository is None:
            return None
        try:
            from tgvio.application.publish_styles import PublishStyleService

            return await PublishStyleService(repository).current(int(owner_id))
        except Exception:
            return None

    async def _on_edit_confirm(self, event, action: str, owner_id: int) -> None:
        editing = await self._editing_service()
        if editing is None:
            await self._safe_answer(event, "编辑功能不可用", alert=True)
            return
        token = action.split(":", 2)[2] if action.count(":") >= 2 else ""
        if not token:
            await self._safe_answer(event, "确认已过期", alert=True)
            return
        await self._safe_answer(event, "正在发布合集")
        try:
            result = await editing.confirm(
                owner_id=owner_id,
                chat_id=int(event.chat_id),
                token=token,
                destination=self._settings.destination,
                max_items=min(100, int(getattr(self._settings, "batch_max_items", 100))),
                ask_timeout_seconds=int(
                    getattr(self._settings, "spoiler_confirm_timeout_seconds", 60)
                ),
                style_policy=await self._resolve_style(owner_id),
            )
        except DraftRevisionConflict:
            await self._safe_send(event.chat_id, "⚠️ 内容已变化，请重新预览后再确认。")
            return
        except (DraftUnavailableError, OperationTokenInvalidError):
            await self._safe_send(event.chat_id, "⚠️ 该确认已过期或已被使用。")
            return
        except Exception as exc:  # pragma: no cover - defensive
            await self._safe_send(event.chat_id, f"⚠️ 发布未完成：{type(exc).__name__}")
            return
        await self._announce_finalize_result(event.chat_id, owner_id, result, confirmation=True)

    # ------------------------------------------------------------ draft list
    async def _on_draft_list(self, event, action: str, owner_id: int) -> None:
        editing = await self._editing_service()
        if editing is None:
            await self._safe_answer(event, "草稿功能不可用", alert=True)
            return
        parts = action.split(":")
        if len(parts) < 4:
            await self._safe_answer(event, "操作已过期", alert=True)
            return
        operation = parts[2]
        value = parts[3]
        try:
            if operation == "l":
                await self._safe_answer(event, "已刷新")
                await self._render_draft_list(event.chat_id, owner_id, page=int(value))
                return
            if operation == "a":
                draft = await editing.activate(owner_id=owner_id, session_id=value)
                await self._safe_answer(event, "已打开草稿")
                await self._render_edit_panel(event.chat_id, owner_id, value, draft)
                return
            if operation == "d":
                await editing.discard(owner_id=owner_id, session_id=value)
                await self._safe_answer(event, "草稿已删除")
                await self._render_draft_list(event.chat_id, owner_id)
                return
        except (DraftUnavailableError, DraftRevisionConflict):
            await self._safe_answer(event, "草稿已失效", alert=True)
            return
        await self._safe_answer(event, "操作已过期", alert=True)

    async def _render_draft_list(self, chat_id: int, owner_id: int, *, page: int = 0) -> None:
        editing = await self._editing_service()
        if editing is None:
            return
        drafts = await editing.repository.list_drafts(int(owner_id), limit=20)
        page_size = _PAGE_SIZE
        total_pages = max(1, (len(drafts) + page_size - 1) // page_size)
        page = max(0, min(int(page), total_pages - 1))
        window = drafts[page * page_size : (page + 1) * page_size]
        lines = [f"📝 **我的草稿** · 第 `{page + 1}/{total_pages}` 页", "──────────"]
        if not drafts:
            lines.append("还没有保存的草稿。")
        for index, draft in enumerate(window, start=1):
            entries = await editing.entries(draft.session_id)
            media = sum(1 for item in entries if item.entry.kind == CollectionEntryKind.MEDIA and not item.excluded)
            lines.append(
                f"{index}. {'✏️' if draft.active else '💾'} {draft.state.value} · {media} 个媒体 · {draft.updated_at or ''}"
            )
        rows: list[list] = []
        for index, draft in enumerate(window, start=1):
            rows.append(
                [
                    Button.inline(
                        f"{index} ✏️ 继续",
                        f"intake:df:a:{draft.session_id}".encode("utf-8"),
                    ),
                    Button.inline(
                        f"{index} 🗑 删除",
                        f"intake:df:d:{draft.session_id}".encode("utf-8"),
                    ),
                ]
            )
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(Button.inline("⬅️", f"intake:df:l:{page - 1}".encode("utf-8")))
            nav.append(Button.inline("🔄", f"intake:df:l:{page}".encode("utf-8")))
            if page + 1 < total_pages:
                nav.append(Button.inline("➡️", f"intake:df:l:{page + 1}".encode("utf-8")))
            rows.append(nav)
        rows.append([Button.inline("🏠 首页", b"ui:home")])
        await self._safe_send(chat_id, "\n".join(lines), buttons=rows)

    # --------------------------------------------------------- caption intake
    async def _apply_pending_caption(self, owner_id: int, chat_id: int, text: str) -> bool:
        editing = await self._editing_service()
        if editing is None:
            return False
        try:
            draft = await editing.apply_pending_caption(
                owner_id=owner_id, chat_id=int(chat_id), text=text
            )
        except Exception:
            return False
        if draft is None:
            return False
        await self._safe_send(chat_id, "✅ 文案已更新。")
        await self._render_edit_panel(chat_id, owner_id, draft.session_id, draft)
        return True

    async def _show_drafts(self, chat_id: int, owner_id: int) -> None:
        if not self._editing_enabled():
            await self._safe_send(chat_id, "草稿功能当前未启用。")
            return
        await self._render_draft_list(chat_id, owner_id)
