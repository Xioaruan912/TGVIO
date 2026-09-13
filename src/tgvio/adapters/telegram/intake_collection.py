from __future__ import annotations

from tgvio.adapters.telegram.intake_runtime_support import *  # noqa: F401,F403


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
        if action == COLLECTION_BEGIN_BUTTON:
            await self._begin_collection(event.chat_id, int(event.sender_id))
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
        if session.status_message_id is None:
            message = await self._safe_send(chat_id, text, buttons=buttons)
            message_id = getattr(message, "id", None)
            if message_id is not None:
                await self._repository.set_collection_status_message(
                    session.id,
                    int(chat_id),
                    int(message_id),
                )
        else:
            await self._safe_edit(
                int(session.status_chat_id or chat_id),
                int(session.status_message_id),
                text,
                buttons=buttons,
            )

    async def _end_collection(self, chat_id: int, owner_id: int) -> None:
        if not bool(getattr(self._settings, "collections_enabled", True)):
            await self._safe_send(chat_id, "合集功能当前未启用。")
            return
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
                # A previous /end attempt may have committed this Job but
                # crashed before scheduling it. Durable phase claims make
                # recovery safe even if another local task is already active.
                await self.recover(accepted.job)
        if activated == 0 and result.session.status_message_id is not None:
            await self._safe_edit(
                int(result.session.status_chat_id or chat_id),
                int(result.session.status_message_id),
                "✅ **合集已结束**\n没有新的媒体需要创建任务；重复 update 已忽略。",
            )

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
            "继续发送图片、视频或文字；完成后点“结束并发布”或发送 /end。"
        )

    @staticmethod
    def _collection_buttons(session_id: str):
        return [
            [Button.inline("🛑 结束并发布", f"intake:end:{session_id}".encode("utf-8"))],
            [Button.inline("❌ 取消合集", f"intake:collection-cancel:{session_id}".encode("utf-8"))],
        ]
