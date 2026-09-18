from __future__ import annotations

from tgvio.adapters.telegram.intake_runtime_support import *  # noqa: F401,F403
from tgvio.domain.telegram_links import is_telegram_link


class IntakeSourceMixin:
    """Owner-driven personal-session intake: reply trigger and Telegram links.

    The owner session is optional; when it is not configured none of these
    handlers are registered and every method is a no-op.
    """

    def register_source_handlers(self, client) -> None:
        reader = getattr(self, "_source_reader", None)
        if client is None or reader is None:
            return
        client.add_event_handler(
            self._on_source_trigger,
            events.NewMessage(outgoing=True, func=reader.is_trigger),
        )

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

    async def _on_source_trigger(self, event) -> None:
        reader = getattr(self, "_source_reader", None)
        if reader is None:
            return
        owner_id = getattr(self, "_source_owner_id", None) or getattr(event, "sender_id", None)
        if not owner_id:
            return
        try:
            media = await reader.capture_reply(event)
            if media:
                await self._accept_and_schedule(int(owner_id), int(owner_id), media)
            else:
                await self._safe_send(
                    int(owner_id),
                    "⚠️ 未能读取被回复的消息：可能已被删除、不是媒体，或不在白名单。",
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
            if bool(getattr(self._settings, "source_delete_trigger", True)):
                await self._delete_source_trigger(event)

    async def _delete_source_trigger(self, event) -> None:
        client = getattr(self, "_source_client", None)
        if client is None:
            return
        try:
            await client.delete_messages(event.chat_id, [int(event.message.id)])
        except Exception:  # noqa: BLE001 - cleanup must never fail the capture
            pass
