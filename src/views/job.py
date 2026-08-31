"""Unified task status card renderer."""

from __future__ import annotations

from dataclasses import dataclass

from telethon import Button

from ..progress import render_bar


def _size(value: int | None) -> str:
    if not value:
        return "大小未知"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "大小未知"


def _duration(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    if seconds > 86400:
        return "较长"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    return f"{minutes} 分 {sec} 秒" if sec else f"{minutes} 分"


@dataclass(frozen=True)
class JobCardView:
    seq: int
    phase: str
    media_count: int = 1
    total_bytes: int | None = None
    transferred_bytes: int | None = None
    pct: int | None = None
    speed_bps: float | None = None
    eta_seconds: float | None = None
    item: int = 1
    items: int = 1
    show_progress: bool = True
    error: str | None = None
    cache_retained: bool = False
    compat_note: str = ""


_PHASE = {
    "queued": ("⏳", "等待下载"),
    "downloading": ("⬇️", "正在下载"),
    "ready": ("⏳", "等待发布"),
    "publishing": ("📤", "正在发布"),
    "succeeded": ("✅", "已发布"),
    "failed": ("❌", "失败"),
    "paused": ("⏸", "已暂停"),
    "cancelled": ("⚠️", "已取消"),
}


def job_card_view(state: JobCardView) -> tuple[str, list]:
    icon, label = _PHASE.get(state.phase, ("⏳", state.phase))
    lines = [f"{icon} 任务 #{state.seq} · {label}", "──────────"]
    media = f"🎬 {max(1, state.media_count)} 个媒体"
    if state.total_bytes:
        media += f" · {_size(state.total_bytes)}"
    lines.append(media)
    if state.phase in {"downloading", "publishing"}:
        if state.show_progress and state.pct is not None:
            if state.total_bytes:
                lines.append(f"{render_bar(state.pct)} {state.pct}%")
            elif state.transferred_bytes:
                lines.append(f"📥 已传输：{_size(state.transferred_bytes)}")
        if state.speed_bps:
            speed = f"{_size(int(state.speed_bps))}/s"
            eta = _duration(state.eta_seconds)
            lines.append(f"⚡ {speed}" + (f" · 预计剩余 {eta}" if eta else ""))
        if state.items > 1:
            lines.append(f"📍 当前：第 {state.item}/{state.items} 个媒体")
        lines.append("➡️ 下一步：Telegram 发布" if state.phase == "downloading" else "➡️ 下一步：WebDAV 备份（如已启用）")
    elif state.phase == "ready":
        if state.compat_note:
            lines.append(state.compat_note[:300])
        lines.append("➡️ 下一步：Telegram 发布")
    elif state.phase == "succeeded":
        lines.extend(["📢 Telegram：完成", "☁️ WebDAV：后台处理（如已启用）"])
    elif state.phase == "failed":
        lines.append(f"🧾 原因：{(state.error or '未知错误')[:500]}")
        lines.append(f"💾 本地缓存：{'已保留' if state.cache_retained else '不可用或未知'}")
    buttons: list[list] = []
    if state.phase == "downloading":
        buttons.append([Button.inline("⏸ 暂停", f"hold:{state.seq}"), Button.inline("✖️ 取消", f"q_cancel:{state.seq}")])
    elif state.phase == "ready":
        buttons.append([Button.inline("⏸ 暂停", f"hold:{state.seq}"), Button.inline("✖️ 取消", f"q_cancel:{state.seq}")])
    elif state.phase == "publishing":
        buttons.append([Button.inline("✖️ 取消", f"stop:{state.seq}")])
    elif state.phase == "failed":
        buttons.append([Button.inline("🔄 重试", f"retry:{state.seq}")])
    elif state.phase == "succeeded":
        buttons.append([Button.inline("↩️ 撤销发布", f"undo:{state.seq}")])
    buttons.append([Button.inline("📋 查看队列", "h:q"), Button.inline("🏠 首页", "h:r")])
    return "\n".join(lines), buttons
