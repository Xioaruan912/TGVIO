"""Stable /start home console renderer."""

from __future__ import annotations

from dataclasses import dataclass

from telethon import Button


@dataclass(frozen=True)
class HomeViewState:
    session_active: bool = False
    session_media: int = 0
    session_texts: int = 0
    running: int = 0
    waiting: int = 0
    failed: int = 0
    paused: bool = False
    webdav_enabled: bool = False
    webdav_health: str = "未配置"
    disk_used_gb: float | None = None
    disk_total_gb: float | None = None
    disk_reserved_gb: float = 0.0
    disk_protected_gb: float = 0.0
    disk_reclaimable_gb: float = 0.0
    disk_enforce: bool = False
    destination_profile: str = "默认频道"


def home_view(state: HomeViewState) -> tuple[str, list]:
    if state.session_active:
        session = f"{state.session_media} 个媒体 · {state.session_texts} 条文字"
    else:
        session = "未开始"
    paused = " · 全局暂停" if state.paused else ""
    webdav = (
        f"已启用 · {state.webdav_health}"
        if state.webdav_enabled
        else "未启用"
    )
    if state.disk_used_gb is None or state.disk_total_gb is None:
        disk = "未知"
    else:
        mode = "强制" if state.disk_enforce else "监控"
        disk = (
            f"{state.disk_used_gb:.1f} / {state.disk_total_gb:.1f} GB · {mode}\n"
            f"   预留 {state.disk_reserved_gb:.2f} GB · 保护 {state.disk_protected_gb:.2f} GB · 可清理 {state.disk_reclaimable_gb:.2f} GB"
        )
    text = "\n".join(
        [
            "🤖 Telegram 媒体中转站",
            "──────────",
            f"📥 当前合集：{session}",
            f"📋 任务队列：{state.running} 运行 · {state.waiting} 等待 · {state.failed} 失败{paused}",
            f"🎯 发布目的地：{state.destination_profile[:48]}",
            f"☁️ WebDAV：{webdav}",
            f"💾 磁盘：{disk}",
            "──────────",
            "请选择一个操作",
        ]
    )
    buttons = [
        [Button.inline("➕ 开始合集", "h:begin"), Button.inline("🛑 结束合集", "h:end")],
        [Button.inline("📋 任务队列", "h:q"), Button.inline("❌ 失败任务", "h:f")],
        [Button.inline("🎯 发布目的地", "h:dp"), Button.inline("📡 自动来源", "h:sp")],
        [Button.inline("☁️ 备份管理", "h:w"), Button.inline("🔐 Dashboard", "h:dashboard")],
        [Button.inline("⚙️ 设置", "h:s")],
        [Button.inline("📊 运行状态", "h:status"), Button.inline("❓ 帮助", "h:help")],
        [Button.inline("🔄 刷新", "h:r")],
    ]
    return text, buttons


def home_button() -> list:
    return [Button.inline("🏠 首页", "h:r")]
