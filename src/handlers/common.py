"""Shared Telegram handler context and safe response helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Awaitable, Callable, Pattern

from ..services import (
    BackupManager,
    DestinationProfileManager,
    InteractionSessions,
    JobQueue,
    OperationStore,
    ProxyManager,
    SourceProfileManager,
    StatsService,
)


logger = logging.getLogger(__name__)


@dataclass
class HandlerContext:
    client: Any
    pipeline: Any
    queue: JobQueue
    backup: BackupManager
    proxy: ProxyManager
    interactions: InteractionSessions
    operations: OperationStore
    stats: StatsService | None
    allowed_users: set[int]
    auto_delete_seconds: float
    session_collect: bool
    media_types: tuple[type, ...]
    url_re: Pattern[str]
    dest_channel: str
    start_text: str
    about_text: str
    delete_after: Callable[[object, float], Awaitable[None]]
    destinations: DestinationProfileManager | None = None
    sources: SourceProfileManager | None = None

    def authorized(self, event: Any) -> bool:
        return event.sender_id in self.allowed_users

    async def respond(
        self,
        event: Any,
        text: str,
        auto_delete: bool = True,
        **kwargs: Any,
    ) -> object | None:
        try:
            message = await event.respond(text, **kwargs)
            if auto_delete and self.auto_delete_seconds > 0:
                asyncio.get_running_loop().create_task(
                    self.delete_after(message, self.auto_delete_seconds)
                )
            return message
        except Exception as exc:
            logger.warning("Respond failed: %s", exc)
            return None

    @staticmethod
    async def answer(event: Any, text: str = "") -> None:
        try:
            await event.answer(text)
        except Exception:
            pass

    @staticmethod
    async def edit(event: Any, text: str, **kwargs: Any) -> None:
        try:
            await event.edit(text, **kwargs)
        except Exception:
            pass

    @staticmethod
    def spawn(coro: Awaitable[Any]) -> asyncio.Task:
        return asyncio.get_running_loop().create_task(coro)

