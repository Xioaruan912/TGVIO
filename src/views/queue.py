"""Queue management renderer.

This module receives immutable values only. It must not read or mutate pipeline
state directly; orchestration code is responsible for constructing the model.
"""

from dataclasses import dataclass

from telethon import Button

from ..progress import position_token, render_bar


@dataclass(frozen=True)
class QueueItemView:
    seq: int
    position: int
    state: str
    pct: int | None = None
    item: int = 1
    items: int = 1


@dataclass(frozen=True)
class PendingQueueItemView:
    seq: int
    position: int
    kind: str


@dataclass(frozen=True)
class QueueViewState:
    show_progress: bool
    active: tuple[QueueItemView, ...] = ()
    pending: tuple[PendingQueueItemView, ...] = ()
    has_sessions: bool = False
    session_media: int = 0
    session_texts: int = 0


def _task_label(position: int) -> str:
    return f"队列第 {position} 位"


def _render_active(item: QueueItemView, show_progress: bool) -> str:
    pos = _task_label(item.position)
    if item.state == "download":
        if item.pct is not None and show_progress:
            prefix = (
                f"⬇ 下载 {item.item}/{item.items}"
                if item.items > 1
                else "🔄 正在下载"
            )
            return f"{pos} {prefix} {render_bar(item.pct)} {item.pct:3d}%"
        return f"{pos} 🔄 正在下载"
    if item.state == "upload":
        if item.pct is not None and show_progress:
            prefix = (
                f"📤 上传 {item.item}/{item.items}"
                if item.items > 1
                else "📤 正在上传"
            )
            return f"{pos} {prefix} {render_bar(item.pct)} {item.pct:3d}%"
        return f"{pos} 📤 正在上传"
    if item.state == "paused":
        return f"{pos} ⏸ 已暂停"
    if item.state == "ready":
        return f"{pos} ✅ 等待上传"
    return f"{pos} ⏳ 等待下载"


def queue_view(state: QueueViewState):
    """Render the queue management screen from a read-only view model."""
    lines = ["📋 队列管理", "每个按钮带位置序号，对应下方第 N 位。"]
    buttons = []
    active_lines = []

    for item in state.active:
        active_lines.append(_render_active(item, state.show_progress))
        token = position_token(item.position)
        if item.state == "paused":
            buttons.append(
                [
                    Button.inline(f"{token} ▶ 继续", f"resume:{item.seq}"),
                    Button.inline(f"{token} 🗑 删除", f"q_cancel:{item.seq}"),
                ]
            )
        else:
            buttons.append(
                [
                    Button.inline(f"{token} ⏸ 暂停", f"hold:{item.seq}"),
                    Button.inline(f"{token} ⏹ 取消", f"q_cancel:{item.seq}"),
                ]
            )

    if active_lines:
        lines.append(f"\n▶ 进行中（{len(active_lines)}）")
        lines.extend(active_lines)
    else:
        lines.append("\n▶ 进行中：无")

    pending_lines = []
    for pending in state.pending:
        kind_label = (
            "合集"
            if pending.kind == "collection"
            else "相册"
            if pending.kind == "album"
            else "媒体"
        )
        token = position_token(pending.position)
        pending_lines.append(f"❓{token} 待确认（{kind_label}）")
        buttons.append(
            [Button.inline(f"{token} ❌ 取消", f"cancel:{pending.seq}")]
        )
    if pending_lines:
        lines.append("\n❓ 待确认")
        lines.extend(pending_lines)

    if state.has_sessions:
        lines.append(
            f"\n📦 合集会话进行中：{state.session_media} 个媒体 · "
            f"{state.session_texts} 条评论已收录（发 /end 结束并发布）"
        )

    buttons.append(
        [Button.inline("⏸ 全局暂停", "q_pause"), Button.inline("▶ 全局恢复", "q_resume")]
    )
    buttons.append(
        [Button.inline("/queue", "queue:refresh"), Button.inline("/start", "h:r")]
    )
    return "\n".join(lines), buttons
