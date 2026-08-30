"""Telegram presentation helpers shared by command and callback handlers."""

from telethon import Button
from telethon.tl.types import KeyboardButton, KeyboardButtonRow, ReplyKeyboardMarkup

from .progress import position_token, render_bar


MODE_NAMES = {
    "ask": "每次询问",
    "always_spoiler": "总是雪花遮挡",
    "always_normal": "总是正常",
}

SESSION_BTN_BEGIN = "📥 开始合集"
SESSION_BTN_END = "🛑 结束合集"


def mode_buttons():
    return [
        [Button.inline("🟡 每次询问", "mode:ask")],
        [Button.inline("🔞 总是雪花遮挡", "mode:always_spoiler")],
        [Button.inline("✅ 总是正常", "mode:always_normal")],
    ]


def reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        rows=[
            KeyboardButtonRow(
                buttons=[
                    KeyboardButton(SESSION_BTN_BEGIN),
                    KeyboardButton(SESSION_BTN_END),
                ]
            )
        ],
        resize=True,
        persistent=True,
        single_use=False,
        selective=False,
    )


def queue_view(pipeline, user_id: int):
    """Build the queue management screen without changing pipeline state."""
    lines = ["📋 队列管理", "每个按钮带位置序号，对应下方第 N 位。"]
    buttons = []
    active_lines = []
    show = pipeline._show_progress(user_id)
    for seq in sorted(pipeline.active_seqs):
        pos = pipeline.task_label(seq)
        info = pipeline.active.get(seq)
        if seq in pipeline._download_tasks:
            if info and show:
                prefix = (
                    f"⬇ 下载 {info['item']}/{info['items']}"
                    if info["items"] > 1
                    else "🔄 正在下载"
                )
                state = f"{pos} {prefix} {render_bar(info['pct'])} {info['pct']:3d}%"
            else:
                state = f"{pos} 🔄 正在下载"
        elif seq == pipeline._uploading:
            if info and show:
                prefix = (
                    f"📤 上传 {info['item']}/{info['items']}"
                    if info["items"] > 1
                    else "📤 正在上传"
                )
                state = f"{pos} {prefix} {render_bar(info['pct'])} {info['pct']:3d}%"
            else:
                state = f"{pos} 📤 正在上传"
        elif seq in pipeline._paused_files:
            state = f"{pos} ⏸ 已暂停"
        elif seq in pipeline.jobs:
            state = f"{pos} ✅ 等待上传"
        else:
            state = f"{pos} ⏳ 等待下载"
        active_lines.append(state)
        token = position_token(pipeline._queue_position(seq))
        if seq in pipeline._paused_files:
            buttons.append(
                [
                    Button.inline(f"{token} ▶ 继续", f"resume:{seq}"),
                    Button.inline(f"{token} 🗑 删除", f"q_cancel:{seq}"),
                ]
            )
        else:
            buttons.append(
                [
                    Button.inline(f"{token} ⏸ 暂停", f"hold:{seq}"),
                    Button.inline(f"{token} ⏹ 取消", f"q_cancel:{seq}"),
                ]
            )

    if active_lines:
        lines.append(f"\n▶ 进行中（{len(active_lines)}）")
        lines.extend(active_lines)
    else:
        lines.append("\n▶ 进行中：无")

    pending_lines = []
    for idx, seq in enumerate(sorted(pipeline.pending), start=1):
        pending = pipeline.pending[seq]
        kind_label = "合集" if pending.kind == "collection" else "相册" if pending.kind == "album" else "媒体"
        pending_lines.append(f"❓{position_token(idx)} 待确认（{kind_label}）")
        buttons.append([Button.inline(f"{position_token(idx)} ❌ 取消", f"cancel:{seq}")])
    if pending_lines:
        lines.append("\n❓ 待确认")
        lines.extend(pending_lines)

    if pipeline.sessions:
        total = sum(s.media_count for s in pipeline.sessions.values())
        texts = sum(s.text_count for s in pipeline.sessions.values())
        lines.append(
            f"\n📦 合集会话进行中：{total} 个媒体 · {texts} 条评论已收录"
            "（发 /end 结束并发布）"
        )

    buttons.append(
        [Button.inline("⏸ 全局暂停", "q_pause"), Button.inline("▶ 全局恢复", "q_resume")]
    )
    buttons.append([Button.inline("🔄 刷新队列", "queue:refresh")])
    return "\n".join(lines), buttons
