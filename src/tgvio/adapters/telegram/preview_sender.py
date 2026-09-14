from __future__ import annotations

from pathlib import Path

from telethon import TelegramClient


class TelethonPreviewSender:
    """Sends an effect-preview image to the owner's private chat.

    Spoiler sources are never rendered unblinded: a text notice is sent instead
    so the preview cannot leak未遮挡内容.
    """

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def send_preview(
        self,
        chat_id: int,
        path: Path,
        *,
        spoiler: bool,
        caption: str,
    ) -> None:
        if spoiler:
            await self._client.send_message(
                int(chat_id),
                f"{caption}\n\n🔞 该封面为雪花内容，预览已隐藏；正式发布时会按你的显示设置遮挡。",
            )
            return
        await self._client.send_file(
            int(chat_id),
            str(path),
            caption=caption,
            force_document=False,
        )
