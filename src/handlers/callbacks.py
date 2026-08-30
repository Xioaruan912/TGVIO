"""Central callback action table and dispatcher."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from telethon import events

from .collection import register_collection_callbacks
from .common import HandlerContext
from .jobs import register_job_callbacks
from .proxy import register_proxy_callbacks
from .settings import register_setting_callbacks


CallbackHandler = Callable[[HandlerContext, Any, str], Awaitable[None]]


class CallbackRouter:
    def __init__(self) -> None:
        self._exact: dict[str, CallbackHandler] = {}
        self._prefix: list[tuple[str, CallbackHandler]] = []

    def exact(self, value: str, handler: CallbackHandler) -> None:
        self._exact[value] = handler

    def prefix(self, value: str, handler: CallbackHandler) -> None:
        self._prefix.append((value, handler))

    def resolve(self, data: str) -> CallbackHandler | None:
        handler = self._exact.get(data)
        if handler is not None:
            return handler
        for prefix, candidate in self._prefix:
            if data.startswith(prefix):
                return candidate
        return None


def build_callback_router() -> CallbackRouter:
    router = CallbackRouter()
    register_job_callbacks(router)
    register_setting_callbacks(router)
    register_proxy_callbacks(router)
    register_collection_callbacks(router)
    return router


def register_callback_handler(ctx: HandlerContext) -> CallbackRouter:
    router = build_callback_router()

    @ctx.client.on(events.CallbackQuery())
    async def on_callback(event: events.CallbackQuery.Event) -> None:
        if not ctx.authorized(event):
            await ctx.answer(event, "无权限")
            return
        data = event.data.decode(errors="replace")
        handler = router.resolve(data)
        if handler is None:
            await ctx.answer(event, "操作已过期，请刷新")
            return
        await handler(ctx, event, data)

    return router

