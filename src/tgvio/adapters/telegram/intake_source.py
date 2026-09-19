from __future__ import annotations

from tgvio.adapters.telegram.intake_runtime_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.source_runtime import SourceLoginError
from tgvio.domain.telegram_links import is_telegram_link

_CANCEL_WORDS = {"取消", "/cancel", "cancel", "退出"}


class IntakeSourceMixin:
    """Owner-driven personal-session intake: login input, links and grabbing.

    The source session is optional; while it is not ready every method is a
    no-op, and setup prompts are only consumed from the owner's private chat.
    """

    def set_source(self, client, reader, owner_id: int) -> None:
        self._source_reader = reader
        self._source_owner_id = int(owner_id)
        log_event(
            self._log,
            logging.INFO,
            "source.reader.ready",
            "Source reader attached",
            resolved_chats=len(getattr(reader, "allowed_ids", ()) or ()),
        )

    def clear_source(self) -> None:
        self._source_reader = None

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

    async def handle_grab(self, event, argument: str) -> bool:
        """``/grab [来源序号] [photo]``: publish one source's newest media."""

        coordinator = getattr(self, "_source", None)
        if coordinator is None or not hasattr(coordinator, "grab_latest"):
            return False
        tokens = (argument or "").split()
        index = 0
        photo_only = False
        for token in tokens:
            if token.isdigit():
                index = max(0, int(token) - 1)
            elif token.lower() in {"photo", "图片", "图"}:
                photo_only = True
        label = coordinator.source_label(index) or "未配置"
        await self._safe_send(
            event.chat_id,
            f"⏳ 正在抓取 `{label}` 最近一条"
            + ("图片" if photo_only else "视频/文件")
            + "…",
        )
        try:
            count, resolved_label = await coordinator.grab_latest(
                index, photo_only=photo_only
            )
        except Exception as exc:  # noqa: BLE001 - user-facing miss
            log_event(
                self._log,
                logging.WARNING,
                "source.grab.failed",
                "Source grab failed",
                exception_type=type(exc).__name__,
            )
            await self._safe_send(event.chat_id, "⚠️ 抓取失败，请稍后重试。")
            return True
        if count:
            await self._safe_send(
                event.chat_id,
                f"✅ 已抓取 {count} 个媒体（来源：{resolved_label}），正在下载与发布。",
            )
        else:
            await self._safe_send(
                event.chat_id,
                f"⚠️ 来源 `{resolved_label or label}` 最近没有可用媒体；"
                "可用 `/pick` 查看最近内容再点选。",
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
