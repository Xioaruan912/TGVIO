from __future__ import annotations

from tgvio.adapters.telegram.intake_runtime_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.source_runtime import SourceLoginError
from tgvio.domain.telegram_links import is_telegram_link

_CANCEL_WORDS = {"取消", "/cancel", "cancel", "退出"}


class IntakeSourceMixin:
    """Owner-driven personal-session intake: reply trigger, links and setup.

    The source session is optional; while it is not ready every method is a
    no-op, and setup prompts are only consumed from the owner's private chat.
    """

    def set_source(self, client, reader, owner_id: int) -> None:
        self._source_client = client
        self._source_reader = reader
        self._source_owner_id = int(owner_id)
        self.register_source_handlers(client)
        log_event(
            self._log,
            logging.INFO,
            "source.reader.ready",
            "Source reader attached",
            resolved_chats=len(getattr(reader, "allowed_ids", ()) or ()),
        )

    def clear_source(self) -> None:
        self._source_client = None
        self._source_reader = None
        self._source_handler_client = None

    def register_source_handlers(self, client) -> None:
        reader = getattr(self, "_source_reader", None)
        if client is None or reader is None:
            return
        if getattr(self, "_source_handler_client", None) is client:
            return
        # The filter reads the current reader dynamically, so refreshing the
        # whitelist or trigger never needs a second registration.
        client.add_event_handler(
            self._on_source_trigger,
            events.NewMessage(outgoing=True, func=self._is_source_trigger),
        )
        self._source_handler_client = client

    def _is_source_trigger(self, event) -> bool:
        reader = getattr(self, "_source_reader", None)
        return bool(reader is not None and reader.is_trigger(event))

    def source_delete_trigger(self) -> bool:
        coordinator = getattr(self, "_source", None)
        if coordinator is not None and hasattr(coordinator, "effective_delete_trigger"):
            return bool(coordinator.effective_delete_trigger())
        return bool(getattr(self._settings, "source_delete_trigger", True))

    async def handle_source_input(self, raw_text: str, chat_id: int, sender_id: int) -> bool:
        coordinator = getattr(self, "_source", None)
        if coordinator is None or not hasattr(coordinator, "awaiting"):
            return False
        phase = coordinator.awaiting
        if not phase:
            return False
        text = (raw_text or "").strip()
        if not text or text.startswith("/") and text not in _CANCEL_WORDS:
            return False
        if text in _CANCEL_WORDS:
            coordinator.set_awaiting(None)
            await self._safe_send(chat_id, "已取消。")
            return True
        try:
            if phase == "phone":
                reply = await coordinator.request_code(text)
            elif phase == "code":
                reply = await coordinator.submit_code(text)
            elif phase == "password":
                reply = await coordinator.submit_password(text)
            elif phase == "add_chat":
                reply = await coordinator.add_chat(text)
            else:
                coordinator.set_awaiting(None)
                return False
        except SourceLoginError as exc:
            await self._safe_send(chat_id, f"⚠️ {exc}")
            return True
        await self._safe_send(chat_id, reply)
        return True

    async def handle_source_link(self, raw_text: str, chat_id: int, sender_id: int) -> bool:
        reader = getattr(self, "_source_reader", None)
        if reader is None or not raw_text or not is_telegram_link(raw_text):
            return False
        owner_id = getattr(self, "_source_owner_id", None) or sender_id
        media = await reader.resolve_link(raw_text)
        if media:
            await self._accept_and_schedule(int(owner_id), int(owner_id), media)
        else:
            await self._safe_send(
                chat_id,
                "⚠️ 无法读取该 Telegram 链接：可能不是该来源的成员，或消息已删除。",
            )
        return True

    async def accept_source_media(self, owner_id: int, media) -> None:
        await self._accept_and_schedule(int(owner_id), int(owner_id), media)

    async def notify_source_owner(self, text: str) -> None:
        owner_id = getattr(self, "_source_owner_id", None)
        if owner_id is None:
            users = getattr(self._settings, "allowed_users", ()) or ()
            owner_id = users[0] if users else None
        if owner_id is None:
            return
        await self._safe_send(int(owner_id), text)

    async def _on_source_trigger(self, event) -> None:
        coordinator = getattr(self, "_source", None)
        if coordinator is not None and hasattr(coordinator, "handle_trigger"):
            await coordinator.handle_trigger(
                int(event.chat_id),
                int(event.message.id),
                getattr(event.message, "reply_to_msg_id", None),
            )
            return
        await self._on_source_trigger_fallback(event)

    async def _on_source_trigger_fallback(self, event) -> None:
        reader = getattr(self, "_source_reader", None)
        if reader is None:
            return
        owner_id = getattr(self, "_source_owner_id", None) or getattr(event, "sender_id", None)
        if not owner_id:
            return
        has_reply = getattr(getattr(event, "message", None), "reply_to_msg_id", None) is not None
        log_event(
            self._log,
            logging.INFO,
            "source.trigger.received",
            "Source trigger received",
            reply=has_reply,
        )
        try:
            media = await reader.capture_reply(event) if has_reply else []
            if not media and not has_reply and self.source_latest_enabled():
                media = await reader.capture_latest(event.chat_id)
            if media:
                await self._accept_and_schedule(int(owner_id), int(owner_id), media)
            elif has_reply:
                await self._safe_send(
                    int(owner_id),
                    "⚠️ 未能读取被回复的消息：可能已被删除或不是媒体。",
                )
            else:
                await self._safe_send(
                    int(owner_id),
                    "⚠️ 没找到最近的媒体：请**长按目标消息 → 回复**，再发送触发词。",
                )
        except Exception as exc:  # noqa: BLE001 - user-facing miss, never crash the client
            log_event(
                self._log,
                logging.WARNING,
                "source.trigger.failed",
                "Source trigger capture failed",
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            await self._safe_send(int(owner_id), "⚠️ 抓取失败，请稍后重试。")
        finally:
            if self.source_delete_trigger():
                await self._delete_source_trigger(event)

    def source_latest_enabled(self) -> bool:
        coordinator = getattr(self, "_source", None)
        if coordinator is not None and hasattr(coordinator, "effective_latest"):
            return bool(coordinator.effective_latest())
        return bool(getattr(self._settings, "source_latest", True))

    async def handle_trigger_misuse(self, raw_text: str, chat_id: int) -> bool:
        """Explain how to use the trigger when it is sent to the bot itself."""

        coordinator = getattr(self, "_source", None)
        if coordinator is None or not hasattr(coordinator, "effective_trigger"):
            return False
        if (raw_text or "").strip() != coordinator.effective_trigger():
            return False
        await self._safe_send(
            chat_id,
            "💡 触发词要在【来源聊天】里使用：长按目标消息 → 回复 → 发送 "
            f"`{coordinator.effective_trigger()}`。",
        )
        return True

    async def _delete_source_trigger(self, event) -> None:
        client = getattr(self, "_source_client", None)
        if client is None:
            return
        try:
            await client.delete_messages(event.chat_id, [int(event.message.id)])
        except Exception:  # noqa: BLE001 - cleanup must never fail the capture
            pass
