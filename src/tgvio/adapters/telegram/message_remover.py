from __future__ import annotations

from telethon import TelegramClient


class TelethonPublishedMessageRemover:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def delete_message(self, peer_id: int, message_id: int) -> None:
        await self._client.delete_messages(
            int(peer_id),
            [int(message_id)],
            revoke=True,
        )
