"""Queue commands and job-related callback actions."""

from __future__ import annotations

import logging
from typing import Any

from telethon import Button, events

from ..domain import safe_traceback
from ..views import (
    DurableQueueItemView,
    DurableQueuePageView,
    FailureItemView,
    JobDetailViewState,
    batch_actions_view,
    confirmation_view,
    durable_queue_view,
    failure_center_view,
    home_button,
    job_detail_view,
    queue_view as render_queue_view,
)
from .common import HandlerContext


logger = logging.getLogger(__name__)


def _pct(row: dict) -> int | None:
    total = int(row.get("bytes_total") or 0)
    if total <= 0:
        return None
    return max(0, min(100, int(int(row.get("bytes_done") or 0) * 100 / total)))


async def _durable_queue(ctx: HandlerContext, user_id: int, filter_name: str, page: int):
    snapshot = await ctx.queue.durable_queue_page(
        user_id, filter_name=filter_name, page=page, page_size=5
    )
    if snapshot is None:
        return render_queue_view(ctx.queue.view_state(user_id))
    items = tuple(
        DurableQueueItemView(
            job_id=int(row["id"]),
            legacy_seq=row.get("legacy_seq"),
            state=str(row["state"]),
            pct=_pct(row),
            total_items=int(row.get("total_items") or 0),
        )
        for row in snapshot.pop("items")
    )
    return durable_queue_view(DurableQueuePageView(items=items, **snapshot))


async def _detail(ctx: HandlerContext, user_id: int, job_id: int):
    detail = await ctx.queue.durable_job_detail(user_id, job_id)
    if detail is None:
        return None
    backup = detail.get("backup") or {}
    state = JobDetailViewState(
        job_id=int(detail["id"]),
        legacy_seq=detail.get("legacy_seq"),
        revision=int(detail["revision"]),
        kind=str(detail["kind"]),
        state=str(detail["state"]),
        source_kind=str(detail["source_kind"]),
        item_count=int(detail.get("item_count") or 0),
        item_bytes=int(detail.get("item_bytes") or 0),
        bytes_done=int(detail.get("bytes_done") or 0),
        bytes_total=int(detail.get("bytes_total") or 0),
        retry_count=int(detail.get("retry_count") or 0),
        cache_exists=bool(detail.get("cache_exists")),
        published_count=int(detail.get("published_count") or 0),
        backup_state=str(backup.get("state") or detail.get("backup_state") or "disabled"),
        backup_summary=str(detail.get("backup_summary") or ""),
        error_message=str(detail.get("error_message") or ""),
        error_code=str(detail.get("error_code") or ""),
        next_retry_at=detail.get("next_retry_at"),
        accepted_at=float(detail.get("accepted_at") or 0),
        can_retry=bool(detail.get("can_retry")),
        media_compat_summary=str(detail.get("media_compat_summary") or ""),
        destination_profile=str(detail.get("destination_profile") or ""),
    )
    return job_detail_view(state)


async def _failure_center(ctx: HandlerContext, user_id: int, page: int):
    snapshot = await ctx.queue.durable_queue_page(
        user_id, filter_name="failed", page=page, page_size=5
    )
    if snapshot is None:
        return "❌ 失败中心\n当前版本没有持久化失败列表。", [home_button()]
    items = []
    for row in snapshot["items"]:
        detail = await ctx.queue.durable_job_detail(user_id, int(row["id"]))
        if detail is None:
            continue
        items.append(
            FailureItemView(
                job_id=int(row["id"]),
                legacy_seq=row.get("legacy_seq"),
                revision=int(row["revision"]),
                cache_exists=bool(detail.get("cache_exists")),
                error_message=str(detail.get("error_message") or ""),
                can_retry=bool(detail.get("can_retry")),
                error_code=str(detail.get("error_code") or ""),
                next_retry_at=detail.get("next_retry_at"),
            )
        )
    return failure_center_view(tuple(items), page=int(snapshot["page"]), pages=int(snapshot["pages"]))


def register_job_commands(ctx: HandlerContext) -> None:
    @ctx.client.on(events.NewMessage(pattern="/queue$"))
    async def on_queue(event: events.NewMessage.Event) -> None:
        logger.info("CMD /queue from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        text, buttons = await _durable_queue(ctx, event.sender_id, "all", 0)
        await ctx.respond(event, text, buttons=buttons, auto_delete=False)


async def callback_queue_refresh(ctx: HandlerContext, event: Any, _data: str) -> None:
    text, buttons = await _durable_queue(ctx, event.sender_id, "all", 0)
    await ctx.edit(event, text, buttons=buttons)
    await ctx.answer(event, "队列已刷新")


async def callback_queue_page(ctx: HandlerContext, event: Any, data: str) -> None:
    await ctx.answer(event)
    try:
        _, _, filter_name, page_text = data.split(":")
        page = int(page_text)
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    if filter_name not in {"all", "running", "waiting", "paused", "failed", "completed"} or page < 0:
        await ctx.answer(event, "无效筛选")
        return
    if filter_name == "failed" and data.startswith("q:f:"):
        text, buttons = await _failure_center(ctx, event.sender_id, page)
    else:
        text, buttons = await _durable_queue(ctx, event.sender_id, filter_name, page)
    await ctx.edit(event, text, buttons=buttons)


async def callback_job_view(ctx: HandlerContext, event: Any, data: str) -> None:
    await ctx.answer(event)
    try:
        job_id = int(data.split(":", 2)[2])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效任务")
        return
    rendered = await _detail(ctx, event.sender_id, job_id)
    if rendered is None:
        await ctx.answer(event, "任务不存在或已不可见")
        return
    await ctx.edit(event, rendered[0], buttons=rendered[1])


async def callback_job_action(ctx: HandlerContext, event: Any, data: str) -> None:
    await ctx.answer(event)
    try:
        _, action, job_text, revision_text = data.split(":")
        job_id, revision = int(job_text), int(revision_text)
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    if action not in {"c", "r", "d", "u", "h"} or job_id <= 0 or revision <= 0:
        await ctx.answer(event, "无效操作")
        return
    record = await ctx.queue.durable_record(event.sender_id, job_id)
    if record is None:
        await ctx.answer(event, "任务不存在")
        return
    if record.revision != revision:
        await ctx.answer(event, "任务状态已变化，请刷新")
        rendered = await _detail(ctx, event.sender_id, job_id)
        if rendered:
            await ctx.edit(event, rendered[0], buttons=rendered[1])
        return
    if action == "r":
        status, ticket = await ctx.queue.retry_job_by_id(event.sender_id, job_id, revision)
        if status != "ok" or ticket is None:
            await ctx.answer(event, "当前任务不能直接重试")
            return
        try:
            new_status = await event.client.send_message(
                event.chat_id, f"🔄 {ctx.queue.task_label(ticket.new_seq)} 已重新入队"
            )
        except Exception:
            new_status = ticket.info.job.status
        ctx.queue.commit_retry(ticket, new_status)
        await ctx.answer(event, "已重新入队")
        text, buttons = await _durable_queue(ctx, event.sender_id, "all", 0)
        await ctx.edit(event, text, buttons=buttons)
        return
    operation_action = {
        "c": "cancel",
        "d": "delete_cache",
        "u": "undo",
        "h": "delete_history",
    }[action]
    operation = ctx.operations.create(
        user_id=event.sender_id,
        action=operation_action,
        job_id=job_id,
        expected_revision=revision,
    )
    seq = record.legacy_seq if record.legacy_seq is not None else record.id
    text, buttons = confirmation_view(
        operation_id=operation.operation_id,
        action=operation.action,
        label=f"#{seq}",
    )
    await ctx.edit(event, text, buttons=buttons)


async def callback_confirm_operation(ctx: HandlerContext, event: Any, data: str) -> None:
    await ctx.answer(event)
    try:
        _, choice, operation_text = data.split(":")
        operation_id = int(operation_text)
    except (ValueError, IndexError):
        await ctx.answer(event, "无效确认")
        return
    if choice == "n":
        operation = ctx.operations.consume(operation_id, user_id=event.sender_id)
        await ctx.answer(event, "已取消操作" if operation else "确认已失效")
        if operation:
            rendered = await _detail(ctx, event.sender_id, operation.job_id)
            if rendered:
                await ctx.edit(event, rendered[0], buttons=rendered[1])
        return
    if choice != "y":
        await ctx.answer(event, "无效确认")
        return
    operation = ctx.operations.consume(operation_id, user_id=event.sender_id)
    if operation is None:
        await ctx.answer(event, "确认已过期或无权限")
        return
    if operation.action in {"batch_cancel", "batch_delete_cache"}:
        ok = 0
        skipped = 0
        for job_id, revision in operation.targets:
            if operation.action == "batch_cancel":
                result = await ctx.queue.cancel_job_by_id(event.sender_id, job_id, revision)
            else:
                result = await ctx.queue.delete_failed_cache_by_id(event.sender_id, job_id, revision)
            if result == "ok":
                ok += 1
            else:
                skipped += 1
        await ctx.answer(event, f"批量操作完成：{ok} 成功 · {skipped} 跳过")
        text, buttons = await _durable_queue(ctx, event.sender_id, "all", 0)
        await ctx.edit(event, text, buttons=buttons)
        return
    if operation.action == "cancel":
        result = await ctx.queue.cancel_job_by_id(
            event.sender_id, operation.job_id, operation.expected_revision
        )
        await ctx.answer(event, "已取消任务" if result == "ok" else "任务状态已变化，未执行")
    elif operation.action == "delete_cache":
        result = await ctx.queue.delete_failed_cache_by_id(
            event.sender_id, operation.job_id, operation.expected_revision
        )
        await ctx.answer(event, "缓存已删除" if result == "ok" else "任务状态已变化，未执行")
    elif operation.action == "undo":
        status, refs = await ctx.queue.published_refs_by_id(
            event.sender_id, operation.job_id, operation.expected_revision
        )
        if status != "ok" or not refs:
            await ctx.answer(event, "已没有可撤销的发布消息")
        else:
            deleted = []
            failed = 0
            for ref in refs:
                try:
                    await event.client.delete_messages(ref.peer_id, ref.message_id)
                    deleted.append(ref.id)
                except Exception:
                    failed += 1
            if deleted:
                await ctx.queue.mark_published_deleted_by_id(
                    event.sender_id,
                    operation.job_id,
                    operation.expected_revision,
                    deleted,
                )
            await ctx.answer(event, f"撤销完成：{len(deleted)} 成功 · {failed} 失败")
    elif operation.action == "delete_history":
        result = await ctx.queue.delete_history_by_id(
            event.sender_id,
            operation.job_id,
            operation.expected_revision,
        )
        if result == "ok":
            await ctx.answer(event, "任务历史与关联文字已删除")
            text, buttons = await _durable_queue(ctx, event.sender_id, "all", 0)
            await ctx.edit(event, text, buttons=buttons)
            return
        message = {
            "cache_exists": "请先删除本地缓存",
            "busy": "任务仍有备份/清理操作进行中",
            "active": "任务尚未结束",
            "stale": "任务状态已变化，请刷新",
        }.get(result, "任务不存在或已删除")
        await ctx.answer(event, message)
    rendered = await _detail(ctx, event.sender_id, operation.job_id)
    if rendered:
        await ctx.edit(event, rendered[0], buttons=rendered[1])


async def callback_batch_menu(ctx: HandlerContext, event: Any, data: str) -> None:
    await ctx.answer(event)
    waiting = await ctx.queue.batch_targets(event.sender_id, "waiting")
    failed = await ctx.queue.batch_targets(event.sender_id, "failed")
    failed_bytes = sum(int(row.get("cache_bytes") or 0) for row in failed)
    if data == "q:b":
        text, buttons = batch_actions_view(
            waiting_count=len(waiting),
            failed_count=len(failed),
            failed_bytes=failed_bytes,
        )
        await ctx.edit(event, text, buttons=buttons)
        return
    if data == "q:bc":
        if not waiting:
            await ctx.answer(event, "当前没有等待任务")
            return
        operation = ctx.operations.create_batch(
            user_id=event.sender_id,
            action="batch_cancel",
            targets=[(int(row["id"]), int(row["revision"])) for row in waiting],
        )
        text, buttons = confirmation_view(
            operation_id=operation.operation_id,
            action=operation.action,
            label=f"{len(waiting)} 个等待任务",
        )
        await ctx.edit(event, text, buttons=buttons)
        return
    if data == "q:bd":
        targets = [row for row in failed if int(row.get("cache_bytes") or 0) > 0]
        total_bytes = sum(int(row.get("cache_bytes") or 0) for row in targets)
        if not targets:
            await ctx.answer(event, "当前没有可清理的失败缓存")
            return
        operation = ctx.operations.create_batch(
            user_id=event.sender_id,
            action="batch_delete_cache",
            targets=[(int(row["id"]), int(row["revision"])) for row in targets],
            total_bytes=total_bytes,
        )
        text, buttons = confirmation_view(
            operation_id=operation.operation_id,
            action=operation.action,
            label=f"{len(targets)} 个失败任务 · {total_bytes / 1024 / 1024:.1f} MB",
        )
        await ctx.edit(event, text, buttons=buttons)
        return


async def callback_cancel_pending(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    ok = await ctx.queue.cancel_pending(seq, user_id=event.sender_id)
    await ctx.answer(event, "已取消" if ok else "该确认已失效")


async def callback_cancel_job(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    job_id = ctx.queue.durable_job_id(seq)
    if job_id is not None:
        record = await ctx.queue.durable_record(event.sender_id, job_id)
        if record is None:
            await ctx.answer(event, "任务不存在")
            return
        operation = ctx.operations.create(
            user_id=event.sender_id,
            action="cancel",
            job_id=job_id,
            expected_revision=record.revision,
        )
        text, buttons = confirmation_view(
            operation_id=operation.operation_id,
            action="cancel",
            label=f"#{seq}",
        )
        await ctx.edit(event, text, buttons=buttons)
        return
    ok = await ctx.queue.cancel(seq, user_id=event.sender_id)
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
    job_id = ctx.queue.durable_job_id(seq)
    if job_id is not None:
        record = await ctx.queue.durable_record(event.sender_id, job_id)
        if record is None:
            await ctx.answer(event, "任务不存在")
            return
        operation = ctx.operations.create(
            user_id=event.sender_id,
            action="cancel",
            job_id=job_id,
            expected_revision=record.revision,
        )
        text, buttons = confirmation_view(
            operation_id=operation.operation_id,
            action="cancel",
            label=f"#{seq}",
        )
        await ctx.edit(event, text, buttons=buttons)
        return
    if ctx.queue.stop_running(seq, user_id=event.sender_id):
        await ctx.answer(event, "正在停止...")
    else:
        await ctx.answer(event, "该任务不在下载/上传中")


async def callback_undo(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        seq = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    job_id = ctx.queue.durable_job_id(seq)
    if job_id is not None:
        record = await ctx.queue.durable_record(event.sender_id, job_id)
        if record is None:
            await ctx.answer(event, "任务不存在")
            return
        operation = ctx.operations.create(
            user_id=event.sender_id,
            action="undo",
            job_id=job_id,
            expected_revision=record.revision,
        )
        text, buttons = confirmation_view(
            operation_id=operation.operation_id,
            action="undo",
            label=f"#{seq}",
        )
        await ctx.edit(event, text, buttons=buttons)
        return
    ids = ctx.queue.pop_published(seq, user_id=event.sender_id)
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
    job = ctx.queue.hold(seq, user_id=event.sender_id)
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
    job = ctx.queue.resume(seq, user_id=event.sender_id)
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
    ticket = ctx.queue.claim_retry(seq, user_id=event.sender_id)
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
        buttons=home_button(),
    )


async def callback_confirm(ctx: HandlerContext, event: Any, data: str) -> None:
    try:
        _, seq_str, flag_str = data.split(":")
        seq, spoiler = int(seq_str), bool(int(flag_str))
    except (ValueError, IndexError):
        await ctx.answer(event, "无效操作")
        return
    ticket = ctx.queue.claim_confirmation(seq, user_id=event.sender_id)
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
    except Exception as exc:
        logger.error(
            "Enqueue failed for job #%s type=%s traceback=%s",
            seq,
            exc.__class__.__name__,
            safe_traceback(exc),
        )
        ctx.queue.settle_failed_confirmation(seq)
    else:
        logger.info("Job #%s confirmed spoiler=%s", seq, spoiler)
    await ctx.answer(event, "已确认")


def _pending_confirm_text(pending) -> str:
    if pending.kind == "collection":
        return f"⚠️ 该合集（{len(pending.album or [])} 个媒体）是否为 18+？"
    if pending.kind == "album":
        return f"⚠️ 该相册（{len(pending.album or [])} 张）是否为 18+？"
    return "⚠️ 该内容是否为 18+？"


def _pending_confirm_buttons(pending) -> list:
    profile_name = pending.destination_profile_name or "默认频道"
    return [
        [
            Button.inline("🔞 是（雪花遮挡）", f"confirm:{pending.seq}:1"),
            Button.inline("✅ 否", f"confirm:{pending.seq}:0"),
        ],
        [Button.inline(f"🎯 {profile_name[:30]}", f"cp:{pending.seq}")],
        [Button.inline("❌ 取消", f"cancel:{pending.seq}")],
    ]


async def callback_confirm_profile(ctx: HandlerContext, event: Any, data: str) -> None:
    parts = data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await ctx.answer(event, "无效操作")
        return
    seq = int(parts[1])
    pending = ctx.queue.pending_confirmation(seq, user_id=event.sender_id)
    if pending is None:
        await ctx.answer(event, "该确认已失效")
        return
    if ctx.destinations is None:
        await ctx.answer(event, "目的地功能不可用")
        return
    if len(parts) == 2:
        profiles = await ctx.destinations.list_profiles(enabled_only=True)
        buttons = [
            [
                Button.inline(
                    ("✅ " if item.id == pending.destination_profile_id else "") + item.name[:28],
                    f"cp:{seq}:{item.id}",
                )
            ]
            for item in profiles
        ]
        buttons.append([Button.inline("⬅️ 返回确认", f"cp:{seq}:back")])
        await ctx.edit(
            event,
            "🎯 选择本任务发布目的地\n──────────\n选择会写入该任务 snapshot；之后切换默认不会影响它。",
            buttons=buttons,
        )
        return
    if parts[2] == "back":
        await ctx.edit(
            event,
            _pending_confirm_text(pending),
            buttons=_pending_confirm_buttons(pending),
        )
        return
    if not parts[2].isdigit():
        await ctx.answer(event, "无效 profile")
        return
    result = await ctx.queue.select_pending_destination(
        seq, int(parts[2]), user_id=event.sender_id
    )
    if result != "ok":
        await ctx.answer(event, f"切换失败：{result}")
        return
    pending = ctx.queue.pending_confirmation(seq, user_id=event.sender_id)
    await ctx.answer(event, "已切换本任务目的地")
    await ctx.edit(
        event,
        _pending_confirm_text(pending),
        buttons=_pending_confirm_buttons(pending),
    )


def register_job_callbacks(router: Any) -> None:
    router.exact("queue:refresh", callback_queue_refresh)
    router.prefix("q:p:", callback_queue_page)
    router.prefix("q:f:", callback_queue_page)
    router.prefix("j:v:", callback_job_view)
    router.prefix("j:c:", callback_job_action)
    router.prefix("j:r:", callback_job_action)
    router.prefix("j:d:", callback_job_action)
    router.prefix("j:u:", callback_job_action)
    router.prefix("j:h:", callback_job_action)
    router.prefix("x:y:", callback_confirm_operation)
    router.prefix("x:n:", callback_confirm_operation)
    router.exact("q:b", callback_batch_menu)
    router.exact("q:bc", callback_batch_menu)
    router.exact("q:bd", callback_batch_menu)
    router.exact("q_pause", callback_pause_all)
    router.exact("q_resume", callback_resume_all)
    router.exact("toggle_progress", callback_toggle_progress)
    router.prefix("cp:", callback_confirm_profile)
    router.prefix("q_cancel:", callback_cancel_job)
    router.prefix("cancel:", callback_cancel_pending)
    router.prefix("stop:", callback_stop)
    router.prefix("undo:", callback_undo)
    router.prefix("hold:", callback_hold)
    router.prefix("resume:", callback_resume)
    router.prefix("retry:", callback_retry)
    router.prefix("confirm:", callback_confirm)
