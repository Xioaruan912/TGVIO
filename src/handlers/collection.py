"""Collection-session commands and callbacks."""

from __future__ import annotations

import logging
from typing import Any

from telethon import events

from ..domain import safe_traceback
from ..views import reply_keyboard
from .common import HandlerContext


logger = logging.getLogger(__name__)


async def handle_begin(ctx: HandlerContext, event: Any) -> None:
    logger.info("CMD /begin from %s", event.sender_id)
    if not ctx.authorized(event):
        return
    session = ctx.queue.session(event.sender_id)
    if session is not None:
        text = (
            f"📦 合集会话已在进行中（{len(session.items)} 项，"
            f"{session.media_count} 个媒体"
        )
        if session.texts:
            text += f"，{session.text_count} 条评论"
        text += "），转发/评论会自动加入，发 /end 结束"
        await ctx.respond(event, text, buttons=reply_keyboard())
        return
    ctx.queue.begin_session(event.sender_id)
    await ctx.respond(
        event,
        "✅ 合集会话已开始：后续转发（视频/图片）与文字评论将汇总为一个合集，"
        "评论会按行整合为封面文字与封面一起发送。\n"
        "继续转发即可，结束后发 /end 或点状态消息上的「🛑 结束并发布（/end）」按钮发布。",
        buttons=reply_keyboard(),
    )


async def handle_end(ctx: HandlerContext, event: Any) -> None:
    logger.info("CMD /end from %s", event.sender_id)
    if not ctx.authorized(event):
        return
    if not ctx.queue.has_session(event.sender_id):
        await ctx.respond(
            event,
            "当前没有进行中的合集会话（转发内容会自动开始合集）",
            buttons=reply_keyboard(),
        )
        return
    await ctx.respond(
        event,
        "🛑 正在结束合集并发布…",
        auto_delete=False,
        buttons=reply_keyboard(),
    )
    try:
        count = await ctx.queue.finalize_session(event.sender_id, event.chat_id)
        if not count:
            await ctx.respond(event, "合集为空，未发布任何内容")
    except Exception as exc:
        logger.error(
            "Session end failed type=%s traceback=%s",
            exc.__class__.__name__, safe_traceback(exc),
        )
        await ctx.respond(event, f"结束合集失败: {exc}")


def register_collection_commands(ctx: HandlerContext) -> None:
    @ctx.client.on(events.NewMessage(pattern=r"/begin$|/开始$"))
    async def on_begin(event: events.NewMessage.Event) -> None:
        await handle_begin(ctx, event)

    @ctx.client.on(events.NewMessage(pattern=r"/end$|/结束$"))
    async def on_end(event: events.NewMessage.Event) -> None:
        await handle_end(ctx, event)


async def callback_session_end(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        target = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    if target != event.sender_id:
        await ctx.answer(event, "无权限")
        return
    if not ctx.queue.has_session(target):
        await ctx.answer(event, "当前没有进行中的合集")
        return
    await ctx.answer(event, "已结束，正在处理…")
    try:
        await ctx.queue.finalize_session(target, event.chat_id)
    except Exception as exc:
        logger.error(
            "Session end callback failed type=%s traceback=%s",
            exc.__class__.__name__, safe_traceback(exc),
        )
        await ctx.answer(event, f"结束失败: {exc}")


def register_collection_callbacks(router: Any) -> None:
    router.prefix("session_end:", callback_session_end)
