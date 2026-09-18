"""Owner-driven "push" reader backed by the personal-account session.

The owner points at content explicitly — by replying to a message with the
configured trigger, or by sending a Telegram message link — and this reader
turns that single message (plus its media group, when present) into durable
``IncomingMedia`` items. It never scans chats on its own.
"""

from __future__ import annotations

import logging
from typing import Sequence

from telethon import utils

from tgvio.domain.job import MediaKind
from tgvio.domain.telegram_links import TelegramLink, parse_telegram_link
from tgvio.application.intake import IncomingMedia

_ALBUM_SPAN = 11


class UserSourceReader:
    def __init__(
        self,
        client,
        *,
        trigger: str = "#tgvio",
        allowed_chats: Sequence[str] | None = None,
    ) -> None:
        self._client = client
        self._trigger = (trigger or "#tgvio").strip() or "#tgvio"
        self._allowed_raw = tuple(entry for entry in (allowed_chats or ()) if str(entry).strip())
        self._allowed_ids: set[int] = set()
        self._log = logging.getLogger("tgvio.telegram.source")

    @property
    def trigger(self) -> str:
        return self._trigger

    @property
    def allowed_ids(self) -> frozenset[int]:
        return frozenset(self._allowed_ids)

    async def prepare(self) -> int:
        """Resolve the whitelist to durable chat ids; unresolvable entries are skipped."""

        resolved: set[int] = set()
        for entry in self._allowed_raw:
            try:
                entity = await self._client.get_entity(entry)
                resolved.add(int(utils.get_peer_id(entity)))
            except Exception as exc:  # noqa: BLE001 - a bad entry must not abort startup
                self._log.warning(
                    "source whitelist entry could not be resolved: %s", type(exc).__name__
                )
        self._allowed_ids = resolved
        return len(resolved)

    def is_trigger(self, event) -> bool:
        if not getattr(event, "outgoing", False):
            return False
        if (getattr(event, "raw_text", "") or "").strip() != self._trigger:
            return False
        if getattr(getattr(event, "message", None), "reply_to_msg_id", None) is None:
            return False
        chat_id = getattr(event, "chat_id", None)
        return chat_id is not None and int(chat_id) in self._allowed_ids

    async def capture_reply(self, event) -> list[IncomingMedia]:
        reply = await event.get_reply_message()
        if reply is None:
            return []
        return self._to_media_list(await self._expand(reply.message))

    async def resolve_link(self, url: str) -> list[IncomingMedia]:
        link: TelegramLink | None = parse_telegram_link(url)
        if link is None:
            return []
        try:
            entity = await self._client.get_entity(link.chat)
            message = await self._client.get_messages(entity, ids=link.message_id)
        except Exception as exc:  # noqa: BLE001 - unreadable link is a user-facing miss
            self._log.warning("source link could not be resolved: %s", type(exc).__name__)
            return []
        if message is None:
            return []
        return self._to_media_list(await self._expand(message))

    async def _expand(self, message):
        grouped = getattr(message, "grouped_id", None)
        if not grouped:
            return [message]
        try:
            nearby = [
                item
                async for item in self._client.iter_messages(
                    message.chat_id,
                    min_id=max(0, int(message.id) - _ALBUM_SPAN),
                    max_id=int(message.id) + _ALBUM_SPAN,
                )
            ]
        except Exception:  # noqa: BLE001 - fall back to the single replied message
            return [message]
        album = [item for item in nearby if getattr(item, "grouped_id", None) == grouped]
        album.sort(key=lambda item: int(item.id))
        return album or [message]

    def _to_media_list(self, messages) -> list[IncomingMedia]:
        media: list[IncomingMedia] = []
        for message in messages:
            item = self._to_media(message)
            if item is not None:
                media.append(item)
        return media

    @staticmethod
    def _to_media(message) -> IncomingMedia | None:
        if getattr(message, "photo", None) is not None:
            kind = MediaKind.PHOTO
        elif getattr(message, "video", None) is not None:
            kind = MediaKind.VIDEO
        elif getattr(message, "audio", None) is not None:
            kind = MediaKind.AUDIO
        elif getattr(message, "voice", None) is not None:
            kind = MediaKind.AUDIO
        elif getattr(message, "document", None) is not None:
            kind = MediaKind.DOCUMENT
        else:
            return None
        file_info = getattr(message, "file", None)
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if chat_id is None or message_id is None:
            return None
        return IncomingMedia(
            kind=kind,
            source=f"telegram:{int(chat_id)}:{int(message_id)}",
            caption=str(getattr(message, "message", "") or ""),
            size_bytes=int(getattr(file_info, "size", 0) or 0),
            name=getattr(file_info, "name", None),
            spoiler=bool(getattr(getattr(message, "media", None), "spoiler", False)),
            grouped_id=getattr(message, "grouped_id", None),
            source_chat_id=int(chat_id),
            source_message_id=int(message_id),
            metadata={"source_type": "user_source", "user_source": True},
        )
