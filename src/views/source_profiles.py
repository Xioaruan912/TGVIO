"""S1 source-profile views."""

from __future__ import annotations

from dataclasses import dataclass

from telethon import Button


@dataclass(frozen=True)
class SourceProfileView:
    profile_id: int
    name: str
    source_peer: str
    destination_name: str
    enabled: bool
    spoiler_policy: str
    caption_policy: str
    backup_policy: str
    album_gather_seconds: float
    sequential_video_gather_seconds: float


def source_profiles_view(items: tuple[SourceProfileView, ...]) -> tuple[str, list]:
    lines = [
        "📡 自动来源",
        "──────────",
        "只处理 Bot 成为来源成员后收到的新消息；重启仅恢复已接收的精确消息。",
    ]
    if not items:
        lines.append("\n暂无来源 Profile。")
    else:
        for item in items:
            lines.append(
                f"\n{'🟢' if item.enabled else '⚪️'} {item.name[:32]}\n"
                f"来源：{item.source_peer[:48]}\n目的地：{item.destination_name[:48]}"
            )
    buttons = [
        [Button.inline(("🟢 " if item.enabled else "⚪️ ") + item.name[:24], f"sp:v:{item.profile_id}")]
        for item in items
    ]
    buttons.append([Button.inline("➕ 新建来源", "sp:add")])
    buttons.append([Button.inline("🔄 刷新", "sp:r"), Button.inline("🏠 首页", "h:r")])
    return "\n".join(lines), buttons


def source_profile_detail_view(item: SourceProfileView) -> tuple[str, list]:
    lines = [
        f"📡 {item.name}",
        "──────────",
        f"状态：{'🟢 已启用' if item.enabled else '⚪️ 已禁用'}",
        f"来源：{item.source_peer[:80]}",
        f"目的地：{item.destination_name[:80]}",
        f"原生媒体组等待：{item.album_gather_seconds:g} 秒",
        f"连续单视频等待：{item.sequential_video_gather_seconds:g} 秒（满 10 个立即提交）",
        f"18+：{item.spoiler_policy}",
        f"Caption：{item.caption_policy}",
        f"备份：{item.backup_policy}",
        "",
        "只接收新 update；编辑/删除源消息不会反向同步。",
    ]
    buttons = [
        [Button.inline("⏸ 禁用" if item.enabled else "▶️ 启用", f"sp:tg:{item.profile_id}")],
        [Button.inline("🔎 检查访问", f"sp:chk:{item.profile_id}")],
        [Button.inline("🎯 目的地", f"sp:dst:{item.profile_id}"), Button.inline("🔞 18+", f"sp:spo:{item.profile_id}")],
        [Button.inline("📝 Caption", f"sp:cap:{item.profile_id}"), Button.inline("☁️ 备份", f"sp:bp:{item.profile_id}")],
        [Button.inline("⬅️ 返回", "sp:r"), Button.inline("🏠 首页", "h:r")],
    ]
    return "\n".join(lines), buttons
