from __future__ import annotations

from tgvio.adapters.telegram.intake_runtime_support import *  # noqa: F401,F403


class IntakeMediaMixin:
    """Maps raw Telethon messages into durable ``IncomingMedia`` values."""

    @staticmethod
    def _from_message(message, chat_id: int) -> IncomingMedia | None:
        if message.photo is not None:
            kind = MediaKind.PHOTO
        elif message.video is not None:
            kind = MediaKind.VIDEO
        elif getattr(message, "audio", None) is not None or getattr(message, "voice", None) is not None:
            kind = MediaKind.AUDIO
        elif message.document is not None:
            kind = MediaKind.DOCUMENT
        else:
            return None

        file_info = getattr(message, "file", None)
        original_name = getattr(file_info, "name", None)
        extension = getattr(file_info, "ext", None)
        size = int(getattr(file_info, "size", 0) or 0)
        metadata = {
            "telegram_original_name": original_name,
            "telegram_extension": extension,
        }
        return IncomingMedia(
            kind=kind,
            source=f"telegram:{chat_id}:{message.id}",
            caption=message.message or "",
            size_bytes=size,
            name=original_name,
            spoiler=bool(getattr(message.media, "spoiler", False)),
            grouped_id=message.grouped_id,
            source_chat_id=int(chat_id),
            source_message_id=int(message.id),
            metadata=metadata,
        )

    @staticmethod
    def _from_url_message(message, chat_id: int) -> IncomingMedia | None:
        if getattr(message, "photo", None) is not None or getattr(message, "document", None) is not None:
            return None
        raw = str(getattr(message, "message", "") or "").strip()
        if not raw.lower().startswith(("http://", "https://")):
            return None
        hostname, _port = validate_url_syntax(raw)
        return IncomingMedia(
            kind=MediaKind.DOCUMENT,
            source=f"url:{raw}",
            caption="",
            source_chat_id=int(chat_id),
            source_message_id=int(message.id),
            metadata={
                "source_type": "url",
                "url_hostname": hostname,
            },
        )
