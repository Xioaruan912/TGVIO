"""Queue commands and job-related callback actions."""

from __future__ import annotations

import logging
from typing import Any

from telethon import Button, events

from ..views import home_button, queue_view as render_queue_view
from .common import HandlerContext


logger = logging.getLogger(__name__)


def register_job_commands(ctx: HandlerContext) -> None:
    @ctx.client.on(events.NewMessage(pattern="/queue$"))
    async def on_queue(event: events.NewMessage.Event) -> None:
        logger.info("CMD /queue from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        text, buttons = render_queue_view(ctx.queue.view_state(event.sender_id))
        await ctx.respond(event, text, buttons=buttons, auto_delete=False)


async def callback_queue_refresh(ctx: HandlerContext, event: Any, _data: str) -> None:
    text, buttons = render_queue_view(ctx.queue.view_state(event.sender_id))
    await ctx.edit(event, text, buttons=buttons)
    await ctx.answer(event, "队列已刷新")


async def callback_cancel_pending(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    ok = await ctx.queue.cancel_pending(seq)
    await ctx.answer(event, "已取消" if ok else "该确认已失效")


async def callback_cancel_job(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    ok = await ctx.queue.cancel(seq)
    await ctx.answer(event, "已取消" if ok else "无法取消")


async def callback_pause_all(ctx: HandlerContext, event: Any, _data: str) -> None:
    ctx.queue.pause_all()
    await ctx.answer(event, "已暂停")


async def callback_resume_all(ctx: HandlerContext, event: Any, _data: str) -> None:
    ctx.queue.resume_all()
    await ctx.answer(event, "已恢复")


async def callback_stop(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    if ctx.queue.stop_running(seq):
        await ctx.answer(event, "正在停止...")
    else:
        await ctx.answer(event, "该任务不在下载/上传中")


async def callback_undo(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    ids = ctx.queue.pop_published(seq)
    if not ids:
        await ctx.answer(event, "该发布已无法撤销")
        return
    try:
        if ids and isinstance(ids[0], tuple):
            for peer, mid in ids:
                await event.client.delete_messages(peer, mid)
        else:
            await event.client.delete_messages(ctx.dest_channel, ids)
    except Exception as exc:
        await ctx.answer(event, f"撤销失败: {exc}")
        return
    logger.info("Undo published job #%s ids=%s", seq, ids)
    try:
        await event.delete()
    except Exception:
        pass
    await ctx.answer(event, "已撤销")


async def callback_hold(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    job = ctx.queue.hold(seq)
    if job is not None:
        try:
            await job.status.edit(
                f"⏸ {ctx.queue.task_label(seq)} 已暂停（缓存保留）",
                buttons=[
                    Button.inline("▶ 继续", f"resume:{seq}"),
                    Button.inline("🗑 删除", f"q_cancel:{seq}"),
                ],
            )
        except Exception:
            pass
    await ctx.answer(event, "已暂停")


async def callback_resume(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    job = ctx.queue.resume(seq)
    if job is not None:
        try:
            await job.status.edit(
                f"🔄 {ctx.queue.task_label(seq)} 已继续，等待上传",
                buttons=[
                    Button.inline("⏸ 暂停", f"hold:{seq}"),
                    Button.inline("⏭ 跳过", f"hold:{seq}"),
                    Button.inline("⏹ 取消", f"q_cancel:{seq}"),
                ],
            )
        except Exception:
            pass
    await ctx.answer(event, "已继续")


async def callback_retry(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    ticket = ctx.queue.claim_retry(seq)
    if ticket is None:
        await ctx.answer(event, "该任务已失效（可能已重试）")
        return
    try:
        new_status = await event.client.send_message(
            event.chat_id,
            f"🔄 {ctx.queue.task_label(ticket.new_seq)} 已重新入队",
        )
    except Exception:
        new_status = ticket.info.job.status
    ctx.queue.commit_retry(ticket, new_status)
    await ctx.queue.set_status_reference(ticket.new_seq, new_status, chat_id=event.chat_id)
    await ctx.edit(event, "🔄 已重新入队")
    await ctx.answer(event, "已重新入队")
    logger.info(
        "Job #%s retried as #%s (cached=%s)",
        ticket.old_seq,
        ticket.new_seq,
        bool(ticket.cached_path),
    )


async def callback_toggle_progress(ctx: HandlerContext, event: Any, _data: str) -> None:
    show = ctx.queue.toggle_progress(event.sender_id)
    await ctx.answer(event, "进度条已开启" if show else "进度条已关闭")
    await ctx.edit(
        event,
        "🔔 进度条显示已开启" if show else "🔕 进度条显示已关闭",
        buttons=[home_button()],
    )


async def callback_confirm(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        _, seq_str, flag_str = data.split(":")
        seq, spoiler = int(seq_str), bool(int(flag_str))
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    ticket = ctx.queue.claim_confirmation(seq)
    if ticket is None:
        await ctx.answer(event, "该确认已失效")
        return
    label = "🔞 雪花遮挡" if spoiler else "✅ 正常"
    try:
        new_status = await event.client.send_message(
            event.chat_id,
            f"🔄 {ctx.queue.task_label(seq)} 已确认：{label}",
        )
    except Exception:
        new_status = ticket.pending.status
    else:
        try:
            await event.delete()
        except Exception:
            pass
    try:
        ctx.queue.commit_confirmation(ticket, new_status, spoiler)
        await ctx.queue.set_status_reference(seq, new_status, chat_id=event.chat_id)
    except Exception:
        logger.exception("Enqueue failed for job #%s", seq)
        ctx.queue.settle_failed_confirmation(seq)
    else:
        logger.info("Job #%s confirmed spoiler=%s", seq, spoiler)
    await ctx.answer(event, "已确认")


def register_job_callbacks(router: Any) -> None:
    router.exact("queue:refresh", callback_queue_refresh)
    router.exact("q_pause", callback_pause_all)
    router.exact("q_resume", callback_resume_all)
    router.exact("toggle_progress", callback_toggle_progress)
    router.prefix("q_cancel:", callback_cancel_job)
    router.prefix("cancel:", callback_cancel_pending)
    router.prefix("stop:", callback_stop)
    router.prefix("undo:", callback_undo)
    router.prefix("hold:", callback_hold)
    router.prefix("resume:", callback_resume)
    router.prefix("retry:", callback_retry)
    router.prefix("confirm:", callback_confirm)

