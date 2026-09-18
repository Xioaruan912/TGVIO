from __future__ import annotations

from tgvio.adapters.telegram.intake_runtime_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.bot_ui_support import COLLECTION_NEW_BUTTON, COLLECTION_PREVIEW_BUTTON


class IntakeCollectionMixin:
    async def _open_collection(self, sender_id: int, chat_id: int):
        if not bool(getattr(self._settings, "collections_enabled", True)):
            return None
        if not hasattr(self._intake, "open_collection"):
            return None
        return await self._intake.open_collection(
            owner_id=int(sender_id),
            chat_id=int(chat_id),
        )

    async def _on_collection_button(self, event) -> None:
        if not self._authorized(event.sender_id):
            return
        action = (event.raw_text or "").strip()
        if action in {COLLECTION_BEGIN_BUTTON, COLLECTION_NEW_BUTTON}:
            await self._begin_collection(event.chat_id, int(event.sender_id))
        elif action == COLLECTION_PREVIEW_BUTTON:
            await self._request_collection_preview(event.chat_id, int(event.sender_id))
        elif action == COLLECTION_END_BUTTON:
            await self._end_collection(event.chat_id, int(event.sender_id))
        raise events.StopPropagation

    async def _begin_collection(self, chat_id: int, owner_id: int) -> None:
        if not bool(getattr(self._settings, "collections_enabled", True)):
            await self._safe_send(chat_id, "合集功能当前未启用。")
            return
        if not hasattr(self._intake, "begin_collection"):
            await self._safe_send(chat_id, "合集功能当前不可用。")
            return
        key = (int(chat_id), int(owner_id))
        if key in self._pending_batches:
            await self._flush_batch(key)
        session = await self._intake.begin_collection(owner_id=owner_id, chat_id=int(chat_id))
        media_count, text_count = await self._intake.collection_counts(session.id)
        text = self._collection_status_text(media_count, text_count)
        buttons = self._collection_buttons(session.id)
        # Always post a fresh status at the bottom of the chat: editing an older
        # message leaves the user scrolling up to find the collection controls.
        message = await self._safe_send(chat_id, text, buttons=buttons)
        message_id = getattr(message, "id", None)
        if message_id is not None:
            preview_sent = getattr(self, "_preview_sent", None)
            if preview_sent is not None:
                preview_sent.pop(session.id, None)
            await self._repository.set_collection_status_message(
                session.id,
                int(chat_id),
                int(message_id),
            )

    async def _end_collection(self, chat_id: int, owner_id: int) -> None:
        if not bool(getattr(self._settings, "collections_enabled", True)):
            await self._safe_send(chat_id, "合集功能当前未启用。")
            return
        editing_enabled = bool(getattr(self, "_editing_enabled", lambda: False)())
        preview_enabled = bool(getattr(self._settings, "collection_preview_enabled", True))
        flags = getattr(self, "_flags", None)
        if flags is not None:
            try:
                preview_enabled = flags.bool("collection_preview_enabled", preview_enabled)
            except Exception:
                pass
        if preview_enabled or editing_enabled:
            await self._request_collection_preview(chat_id, owner_id)
            return
        await self._confirm_collection(chat_id, owner_id)

    async def _request_collection_preview(self, chat_id: int, owner_id: int) -> None:
        session = await self._open_collection(owner_id, chat_id)
        if session is None:
            await self._safe_send(chat_id, "当前没有正在收集的合集。")
            return
        preference = await self._intake.get_user_preference(owner_id)
        editing = getattr(self, "_editing", None)
        revision = None
        preview = None
        if editing is not None and self._editing_enabled():
            draft = await editing.draft(session.id)
            if draft is not None:
                revision = int(draft.revision)
            preview = await editing.preview(
                session_id=session.id,
                cover_mode=bool(getattr(self._settings, "cover_mode", True)),
            )
        if preview is None:
            preview = await self._intake.preview_collection(
                owner_id=owner_id,
                chat_id=int(chat_id),
                cover_mode=bool(getattr(self._settings, "cover_mode", True)),
            )
        if preview is None or preview.media_count == 0:
            await self._safe_send(chat_id, "合集里还没有媒体；继续发送媒体后再结束。")
            return
        text = self._collection_preview_text(preview, preference.spoiler_mode)
        buttons = self._preview_buttons(session.id, revision=revision)
        preview_sent = getattr(self, "_preview_sent", None)
        active_id = preview_sent.get(session.id) if preview_sent is not None else None
        if active_id is not None and await self._safe_edit(
            chat_id,
            int(active_id),
            text,
            buttons=buttons,
        ):
            return
        previous_chat = int(session.status_chat_id or chat_id)
        previous_id = int(session.status_message_id) if session.status_message_id else None
        message = await self._safe_send(chat_id, text, buttons=buttons)
        message_id = getattr(message, "id", None)
        if message_id is None:
            return
        if preview_sent is not None:
            preview_sent[session.id] = int(message_id)
        await self._repository.set_collection_status_message(
            session.id,
            int(chat_id),
            int(message_id),
        )
        if previous_id is not None and previous_id != int(message_id):
            await self._safe_edit(
                previous_chat,
                previous_id,
                "⏹ **已结束收集**\n预览与确认请见下方最新消息。",
            )

    async def _announce_finalize_result(
        self,
        chat_id: int,
        owner_id: int,
        result,
        *,
        confirmation: bool = False,
    ) -> None:
        reused_status = False
        activated = 0
        for accepted in result.jobs:
            if accepted.job.terminal:
                continue
            activated += 1
            if accepted.created:
                if (
                    not reused_status
                    and result.session.status_message_id is not None
                    and result.session.status_chat_id is not None
                ):
                    await self._save_display_message(
                        accepted.job.id,
                        int(result.session.status_chat_id),
                        int(result.session.status_message_id),
                    )
                    await self._announce_job(
                        int(result.session.status_chat_id),
                        accepted.job,
                        status_message_id=int(result.session.status_message_id),
                    )
                    reused_status = True
                else:
                    await self._announce_job(int(chat_id), accepted.job)
            else:
                # A previous confirm may have committed this Job but crashed
                # before scheduling it. Durable claims make recovery safe.
                await self.recover(accepted.job)
        if activated == 0 and result.session.status_message_id is not None:
            await self._safe_edit(
                int(result.session.status_chat_id or chat_id),
                int(result.session.status_message_id),
                "✅ **合集已结束**\n没有新的媒体需要创建任务；重复 update 已忽略。",
            )

    async def _confirm_collection(self, chat_id: int, owner_id: int) -> None:
        preference = await self._intake.get_user_preference(owner_id)
        try:
            result = await self._intake.finalize_collection(
                owner_id=owner_id,
                chat_id=int(chat_id),
                destination=self._settings.destination,
                max_items=min(100, int(getattr(self._settings, "batch_max_items", 100))),
                spoiler_mode=preference.spoiler_mode,
                ask_timeout_seconds=int(
                    getattr(self._settings, "spoiler_confirm_timeout_seconds", 60)
                ),
            )
        except CollectionEmptyError as exc:
            if "no open" in str(exc):
                await self._safe_send(chat_id, "当前没有正在收集的合集。")
            else:
                await self._safe_send(chat_id, "合集里还没有媒体；继续发送媒体后再结束。")
            return
        await self._announce_finalize_result(chat_id, owner_id, result)

    async def _handle_collection_callback(self, event, action: str, owner_id: int) -> bool:
        if action.startswith(("intake:end:", "intake:preview:")):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return True
            await self._safe_answer(event, "正在生成预览")
            if action.startswith("intake:preview:"):
                await self._request_collection_preview(event.chat_id, owner_id)
            else:
                await self._end_collection(event.chat_id, owner_id)
            return True
        if action.startswith("intake:confirm:"):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return True
            preview_sent = getattr(self, "_preview_sent", None)
            if preview_sent is not None:
                preview_sent.pop(session_id, None)
            await self._safe_answer(event, "正在发布合集")
            await self._confirm_collection(event.chat_id, owner_id)
            return True
        if action.startswith("intake:abandon:"):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return True
            preview_sent = getattr(self, "_preview_sent", None)
            if preview_sent is not None:
                preview_sent.pop(session_id, None)
            await self._intake.cancel_collection(owner_id=owner_id, chat_id=int(event.chat_id))
            await self._safe_answer(event, "合集已放弃")
            if session.status_message_id is not None:
                await self._safe_edit(
                    int(event.chat_id),
                    int(session.status_message_id),
                    "⛔ **合集已放弃**",
                )
            return True
        if action.startswith("intake:prevmode:"):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return True
            preference = await self._intake.get_user_preference(owner_id)
            await self._safe_answer(event, "选择显示模式")
            try:
                await event.edit(
                    self._mode_text(preference.spoiler_mode),
                    buttons=self._mode_buttons(),
                    parse_mode="md",
                )
            except Exception:
                pass
            return True
        if action.startswith("intake:collection-cancel:"):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return True
            preview_sent = getattr(self, "_preview_sent", None)
            if preview_sent is not None:
                preview_sent.pop(session_id, None)
            await self._intake.cancel_collection(owner_id=owner_id, chat_id=int(event.chat_id))
            await self._safe_answer(event, "合集已取消")
            if session.status_message_id is not None:
                await self._safe_edit(
                    int(event.chat_id),
                    int(session.status_message_id),
                    "⛔ **合集已取消**",
                )
            return True
        return False

    async def _show_mode(self, chat_id: int, owner_id: int) -> None:
        preference = await self._intake.get_user_preference(owner_id)
        await self._safe_send(
            chat_id,
            self._mode_text(preference.spoiler_mode),
            buttons=self._mode_buttons(),
        )

    @staticmethod
    def _mode_text(mode: SpoilerMode) -> str:
        labels = {
            SpoilerMode.SOURCE: "跟随原消息",
            SpoilerMode.ASK: "每次询问",
            SpoilerMode.ALWAYS_SPOILER: "总是雪花遮挡",
            SpoilerMode.ALWAYS_NORMAL: "总是正常显示",
        }
        return (
            "**媒体显示模式**\n\n"
            f"当前：`{labels[mode]}`\n\n"
            "默认跟随原消息已有的 spoiler；ask 模式会在任务进入队列后询问一次，超时自动按正常显示处理。"
        )

    @staticmethod
    def _mode_buttons():
        return [
            [Button.inline("↩️ 跟随原消息", b"intake:mode:source")],
            [
                Button.inline("❓ 每次询问", b"intake:mode:ask"),
                Button.inline("🌨️ 总是雪花", b"intake:mode:always_spoiler"),
            ],
            [Button.inline("✅ 总是正常", b"intake:mode:always_normal")],
        ]

    @staticmethod
    def _collection_status_text(media_count: int, text_count: int) -> str:
        return (
            "📥 **合集收集中**\n\n"
            f"媒体：`{media_count}`\n"
            f"文字：`{text_count}`\n\n"
            "继续发送图片、视频或文字；完成后点“预览与整理”，确认后才发布。"
        )

    @staticmethod
    def _collection_buttons(session_id: str):
        encoded = session_id.encode("utf-8")
        return [
            [Button.inline("👀 预览与整理", b"intake:preview:" + encoded)],
            [
                Button.inline("📝 我的草稿", b"intake:df:l:0"),
                Button.inline("❌ 取消合集", b"intake:collection-cancel:" + encoded),
            ],
        ]

    def _collection_preview_text(self, preview, spoiler_mode) -> str:
        labels = {
            SpoilerMode.SOURCE: "跟随原消息",
            SpoilerMode.ASK: "每次询问",
            SpoilerMode.ALWAYS_SPOILER: "总是雪花",
            SpoilerMode.ALWAYS_NORMAL: "总是正常",
        }
        lines = [
            "📦 **合集发布预览**",
            "──────────",
            (
                f"媒体：`{preview.media_count}`"
                f"（图片 `{preview.photo_count}` · 视频 `{preview.video_count}` · 文件 `{preview.document_count}`）"
            ),
            f"体积：约 `{self._human_bytes(preview.total_bytes)}`",
            f"封面：`{preview.cover_plan}`",
        ]
        if preview.discussion_groups:
            lines.append(f"评论区：`{preview.discussion_groups}` 组")
        lines.append(f"文案：`{preview.caption_lines}` 行 · `{preview.caption_chars}` 字")
        lines.append(f"模式：`{labels.get(spoiler_mode, '未知')}`")
        lines.append("──────────")
        lines.append("确认后才会开始下载与发布。")
        return "\n".join(lines)

    def _preview_buttons(self, session_id: str, *, revision: int | None = None):
        encoded = session_id.encode("utf-8")
        if self._editing_enabled() and revision is not None:
            rev = str(int(revision)).encode("utf-8")
            preview_rows = [[Button.inline(
                    "🖼 生成效果预览", b"intake:pv:" + encoded + b":" + rev,
                )]] if getattr(self, "_previews", None) is not None else []
            return preview_rows + [
                [
                    Button.inline(
                        "✏️ 编辑合集",
                        b"intake:ed:" + encoded + b":" + rev + b":o",
                    ),
                    Button.inline(
                        "✅ 确认发布",
                        b"intake:ed:" + encoded + b":" + rev + b":k",
                    ),
                ],
                [
                    Button.inline("🔞 显示模式", b"intake:prevmode:" + encoded),
                    Button.inline("📝 我的草稿", b"intake:df:l:0"),
                ],
                [Button.inline("❌ 放弃", b"intake:abandon:" + encoded)],
            ]
        return [
            [
                Button.inline("✅ 确认发布", b"intake:confirm:" + encoded),
                Button.inline("🔞 显示模式", b"intake:prevmode:" + encoded),
            ],
            [Button.inline("❌ 放弃", b"intake:abandon:" + encoded)],
        ]
