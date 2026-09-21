from __future__ import annotations

from pathlib import Path

from telethon import TelegramClient

from tgvio.config import Settings


class TelethonGateway:
    def __init__(self, settings: Settings, session_dir: Path = Path("session")) -> None:
        self._settings = settings
        session_dir.mkdir(parents=True, exist_ok=True)
        self._client = TelegramClient(
            str(session_dir / "tgvio"),
            settings.api_id,
            settings.api_hash,
            request_retries=8,
            connection_retries=8,
        )
        # Surface the real RPC error instead of Telethon's generic
        # "Request was unsuccessful N time(s)" so failures stay diagnosable.
        self._client._raise_last_call_error = True

    @property
    def client(self) -> TelegramClient:
        return self._client

    async def start(self) -> None:
        await self._client.start(bot_token=self._settings.bot_token)

    async def stop(self) -> None:
        await self._client.disconnect()

