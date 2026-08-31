"""Private-message intake dispatcher."""

from __future__ import annotations

import logging

from telethon import events
from telethon.tl.types import MessageEntityBotCommand

from ..domain import safe_traceback
from ..views import SESSION_BTN_BEGIN, SESSION_BTN_END
from .collection import handle_begin, handle_end
from .common import HandlerContext
from .proxy import handle_proxy_input
from .settings import handle_destination_profile_input, handle_webdav_input
from .source_profiles import handle_source_profile_input


logger = logging.getLogger(__name__)


def register_private_handler(ctx: HandlerContext) -> None:
    @ctx.client.on(events.NewMessage(func=lambda event: event.is_private))
    async def on_private_message(event: events.NewMessage.Event) -> None:
        message = event.message
        logger.info(
            "NewMessage from %s media=%s grouped=%s text_len=%s",
            event.sender_id,
            type(getattr(message, "media", None)).__name__,
            getattr(message, "grouped_id", None),
            len(event.raw_text or ""),
        )
        if not ctx.authorized(event):
            logger.info("Ignoring unauthorized user %s", event.sender_id)
            return

        interaction = ctx.interactions.get(event.sender_id)
        if interaction is not None:
            if interaction.kind == "webdav":
                await handle_webdav_input(ctx, event, interaction)
                return
            if interaction.kind == "proxy":
                await handle_proxy_input(ctx, event, interaction)
                return
            if interaction.kind == "destination_profile":
                await handle_destination_profile_input(ctx, event, interaction)
                return
            if interaction.kind == "source_profile":
                await handle_source_profile_input(ctx, event, interaction)
                return

        if any(
            isinstance(entity, MessageEntityBotCommand)
            for entity in (getattr(message, "entities", None) or [])
        ):
            return

        if isinstance(getattr(message, "media", None), ctx.media_types):
            grouped_id = getattr(message, "grouped_id", None)
            if grouped_id:
                logger.info("Album message -> collect_album gid=%s", grouped_id)
                ctx.queue.collect_album(grouped_id, message, event.chat_id)
                return
            if ctx.session_collect:
                try:
                    await ctx.queue.add_session_batch(
                        event.sender_id,
                        [message],
                        event.chat_id,
                    )
                except Exception as exc:
                    logger.error(
                        "Add single media to session failed type=%s traceback=%s",
                        exc.__class__.__name__, safe_traceback(exc),
                    )
                    await ctx.respond(event, f"加入合集失败: {exc}")
                return
            if ctx.queue.spoiler_mode(event.sender_id) != "ask":
                try:
                    await ctx.queue.auto_enqueue(
                        "media",
                        message,
                        None,
                        event.sender_id,
                    )
                except Exception as exc:
                    logger.error(
                        "Auto-enqueue failed type=%s traceback=%s",
                        exc.__class__.__name__, safe_traceback(exc),
                    )
                    await ctx.respond(event, f"自动处理失败: {exc}")
                return
            seq = ctx.queue.reserve_seq()
            try:
                await ctx.queue.show_confirmation(
                    seq,
                    "media",
                    message,
                    None,
                    event.sender_id,
                    event.chat_id,
                )
            except Exception as exc:
                logger.error(
                    "18+ question failed for #%s type=%s traceback=%s",
                    seq, exc.__class__.__name__, safe_traceback(exc),
                )
                await ctx.respond(event, f"发送确认失败: {exc}")
            return

        url_match = ctx.url_re.search(event.raw_text or "")
        if url_match:
            status = await event.reply("⏳ 正在加入队列...")
            seq = ctx.queue.submit_url(
                status,
                message,
                url_match.group(0),
                event.sender_id,
            )
            await status.edit(f"⏳ {ctx.queue.task_label(seq)} 已加入队列")
            return

        text = (event.raw_text or "").strip()
        if text == SESSION_BTN_BEGIN:
            await handle_begin(ctx, event)
            return
        if text == SESSION_BTN_END:
            await handle_end(ctx, event)
            return
        if ctx.session_collect and text and ctx.queue.has_session(event.sender_id):
            try:
                await ctx.queue.add_session_text(
                    event.sender_id,
                    text,
                    event.chat_id,
                )
            except Exception as exc:
                logger.error(
                    "Add text comment to session failed type=%s traceback=%s",
                    exc.__class__.__name__, safe_traceback(exc),
                )
                await ctx.respond(event, f"添加评论失败: {exc}")
            return

        await ctx.respond(event, "请发送视频或链接，或使用 /start 查看使用说明。")
