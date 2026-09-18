"""Optional personal-account gateway used only to read restricted sources.

The bot token client cannot see chats owned by another bot and cannot read
channel history. This second Telethon client authenticates as the owner's own
account so the reader can fetch exactly the message the owner points at.
"""

from __future__ import annotations

from pathlib import Path

from telethon import TelegramClient

from tgvio.config import Settings


class TelethonUserGateway:
    def __init__(self, settings: Settings, session_path: Path) -> None:
        session_path.parent.mkdir(parents=True, exist_ok=True)
        self._client = TelegramClient(
            str(session_path),
            settings.api_id,
            settings.api_hash,
            request_retries=8,
            connection_retries=8,
        )
        self._user_id: int | None = None

    @property
    def client(self) -> TelegramClient:
        return self._client

    @property
    def user_id(self) -> int | None:
        return self._user_id

    async def start(self) -> None:
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError(
                "source session is not authorized; run scripts/login_source_session.py first"
            )
        me = await self._client.get_me()
        self._user_id = int(getattr(me, "id", 0) or 0)

    async def stop(self) -> None:
        await self._client.disconnect()
