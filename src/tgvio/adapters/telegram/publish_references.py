from __future__ import annotations

import mimetypes
from pathlib import Path

from tgvio.adapters.telegram.publish_transport_support import *  # noqa: F401,F403


class PublishReferenceMixin:
    async def _resolve_reusable_media(self, item: MediaItem):
        reference = item.telegram_ref or ""
        parts = reference.split(":", 2)
        if len(parts) != 3 or parts[0] != "telegram":
            return None
        try:
            chat_id = int(parts[1])
            message_id = int(parts[2])
        except ValueError:
            return None
        try:
            message = await self._client.get_messages(chat_id, ids=message_id)
        except Exception:
            return None
        if message is None:
            return None
        try:
            if item.kind == MediaKind.PHOTO and getattr(message, "photo", None) is not None:
                return types.InputMediaPhoto(
                    id=utils.get_input_photo(message.photo),
                    spoiler=item.spoiler or None,
                )
            document = getattr(message, "document", None)
            if document is not None:
                return types.InputMediaDocument(
                    id=utils.get_input_document(document),
                    spoiler=item.spoiler or None,
                )
        except Exception:
            return None
        return None

    async def _prepare_local_media(
        self,
        source: Path,
        *,
        kind: MediaKind,
        force_document: bool,
        supports_streaming: bool,
        spoiler: bool,
        thumbnail: Path | None = None,
        force_upload: bool = False,
        job_id: str | None = None,
        item_index: int | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_seconds: float | None = None,
    ):
        if (
            kind == MediaKind.PHOTO
            and not force_document
            and not spoiler
            and thumbnail is None
            and not force_upload
        ):
            return str(source)
        uploaded = await self._upload_local_file(
            source,
            job_id=job_id,
            item_index=item_index,
        )
        if kind == MediaKind.PHOTO and not force_document:
            return types.InputMediaUploadedPhoto(
                file=uploaded,
                spoiler=spoiler or None,
            )
        attributes, mime_type = utils.get_attributes(
            str(source),
            force_document=force_document,
            supports_streaming=supports_streaming,
        )
        if kind == MediaKind.VIDEO and not force_document:
            # Telethon parses video metadata with the optional `hachoir` library,
            # which is not installed in the runtime image, so its attributes
            # collapse to duration=0/w=1/h=1. Use the ffprobe facts recorded on
            # the MediaItem instead so Telegram renders a real preview/preview.
            attributes = [
                attribute
                for attribute in attributes
                if not isinstance(attribute, types.DocumentAttributeVideo)
            ]
            attributes.append(
                types.DocumentAttributeVideo(
                    duration=int(duration_seconds or 0),
                    w=int(width or 1),
                    h=int(height or 1),
                    supports_streaming=supports_streaming,
                    round_message=False,
                )
            )
        if kind == MediaKind.AUDIO and not force_document:
            # Publish audio-only downloads as a playable audio media message.
            # Telethon's get_attributes needs an optional tag parser that is not
            # installed in the runtime image, so attach the audio attribute
            # explicitly from the durable duration captured by ffprobe.
            attributes = [
                attribute
                for attribute in attributes
                if not isinstance(attribute, types.DocumentAttributeAudio)
            ]
            attributes.append(
                types.DocumentAttributeAudio(
                    duration=int(duration_seconds or 0),
                    title=(Path(source).stem or None),
                    performer=None,
                    voice=False,
                )
            )
            mime_type = mimetypes.guess_type(str(source))[0] or "audio/mpeg"
        uploaded_thumb = (
            await self._upload_local_file(
                thumbnail,
                job_id=job_id,
                item_index=item_index,
            )
            if thumbnail is not None
            else None
        )
        return types.InputMediaUploadedDocument(
            file=uploaded,
            mime_type=mime_type,
            attributes=attributes,
            force_file=force_document or None,
            spoiler=spoiler or None,
            thumb=uploaded_thumb,
        )
