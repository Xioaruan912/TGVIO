from __future__ import annotations

from tgvio.adapters.telegram.publish_transport_support import *  # noqa: F401,F403


class PublishAlbumMixin:
    async def _send_album(
        self,
        target,
        media: list[object],
        captions: list[str],
        *,
        reply_to: int | None,
        step: PublishStep,
        items: list[MediaItem],
    ) -> list:
        """Send an album while preserving spoiler and existing media refs."""
        single_media: list[types.InputSingleMedia] = []
        for position, current in enumerate(media):
            reference = await self._album_media_reference(target, current)
            single_media.append(
                types.InputSingleMedia(
                    media=reference,
                    random_id=helpers.generate_random_long(),
                    message=captions[position] if position < len(captions) else "",
                )
            )
        try:
            result = await self._client(
                functions.messages.SendMultiMediaRequest(
                    peer=target,
                    multi_media=single_media,
                    reply_to=(
                        types.InputReplyToMessage(reply_to_msg_id=reply_to)
                        if reply_to is not None
                        else None
                    ),
                )
            )
        except (MediaEmptyError, MediaInvalidError):
            # Telegram rejected the album due to invalid or un-groupable media (like GIFs).
            # Fall back to individual sends. Each confirmed result is converted
            # to a receipt immediately so a later failure can never look like a
            # safe, zero-side-effect retry.
            messages = []
            receipts: list[PublishReceipt] = []
            for position, current in enumerate(media):
                try:
                    msg = await self._send_visible_file(
                        target,
                        current,
                        caption=captions[position] if position < len(captions) else "",
                        reply_to=reply_to,
                    )
                    current_receipts = self._receipts(msg, step, [items[position]])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if receipts:
                        raise PublishTransportPartialError(
                            str(exc),
                            tuple(receipts),
                        ) from exc
                    raise PublishTransportUncertainError(str(exc)) from exc
                receipts.extend(current_receipts)
                if isinstance(msg, list):
                    messages.extend(msg)
                else:
                    messages.append(msg)
            return messages
        except Exception as exc:
            raise PublishTransportUncertainError(str(exc)) from exc
        messages = [
            message
            for update in (getattr(result, "updates", None) or [])
            if (message := getattr(update, "message", None)) is not None
            and getattr(message, "id", None) is not None
        ]
        messages.sort(key=lambda message: int(message.id))
        if not messages:
            raise PublishTransportUncertainError(
                "Telegram album send returned no observable message receipts"
            )
        return messages

    async def _album_media_reference(self, peer, media):
        if isinstance(media, (types.InputMediaPhoto, types.InputMediaDocument)):
            return media
        if not isinstance(
            media,
            (types.InputMediaUploadedPhoto, types.InputMediaUploadedDocument),
        ):
            raise TypeError(f"unsupported album media: {type(media).__name__}")
        result = await self._client(
            functions.messages.UploadMediaRequest(peer=peer, media=media)
        )
        if isinstance(result, types.MessageMediaPhoto):
            return types.InputMediaPhoto(
                id=utils.get_input_photo(result.photo),
                spoiler=getattr(media, "spoiler", None),
            )
        if isinstance(result, types.MessageMediaDocument):
            return types.InputMediaDocument(
                id=utils.get_input_document(result.document),
                spoiler=getattr(media, "spoiler", None),
            )
        raise RuntimeError("Telegram uploadMedia returned unsupported album media")
