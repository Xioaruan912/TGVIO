"""DP1 destination profile views."""

from __future__ import annotations

from dataclasses import dataclass

from telethon import Button


@dataclass(frozen=True)
class DestinationProfileView:
    profile_id: int
    name: str
    destination_peer: str
    enabled: bool
    is_default: bool
    read_only: bool
    cover_mode: bool
    forward_caption: bool
    backup_policy: str
    verified: bool


def destination_profiles_view(items: tuple[DestinationProfileView, ...]) -> tuple[str, list]:
    lines = ["🎯 发布目的地", "──────────"]
    if not items:
        lines.append("暂无 profile")
    else:
        for item in items:
            mark = "✅" if item.is_default else ("⏸" if not item.enabled else "•")
            verify = "已验证" if item.verified else "未验证"
            lines.append(f"{mark} {item.name} · {verify} · {item.destination_peer[:48]}")
    buttons = [
        [Button.inline(("✅ " if item.is_default else "") + item.name[:24], f"dp:v:{item.profile_id}")]
        for item in items
    ]
    buttons.append([Button.inline("➕ 新建目的地", "dp:add")])
    buttons.append([Button.inline("/profiles", "dp:r"), Button.inline("/start", "h:r")])
    return "\n".join(lines), buttons


def destination_profile_detail_view(item: DestinationProfileView) -> tuple[str, list]:
    text = "\n".join(
        [
            f"🎯 {item.name}",
            "──────────",
            f"目标：{item.destination_peer[:80]}",
            f"状态：{'启用' if item.enabled else '已禁用'}",
            f"默认：{'是' if item.is_default else '否'}",
            f"验证：{'✅ 已通过测试' if item.verified else '⚠️ 尚未测试'}",
            f"封面模式：{'开启' if item.cover_mode else '关闭'}",
            f"转发 caption：{'开启' if item.forward_caption else '关闭'}",
            f"备份策略：{item.backup_policy}",
            f"来源：{'环境变量兼容 profile（只读）' if item.read_only else '用户 profile'}",
            "──────────",
            "profile 修改只影响之后接受的新任务；已排队任务使用自己的 snapshot。",
        ]
    )
    buttons = []
    if not item.read_only:
        buttons.append([Button.inline("✏️ 编辑", f"dp:e:{item.profile_id}")])
    if item.enabled and item.verified and not item.is_default:
        buttons.append([Button.inline("⭐ 设为默认", f"dp:d:{item.profile_id}")])
    if not item.read_only and item.enabled and not item.is_default:
        buttons.append([Button.inline("⏸ 禁用", f"dp:x:{item.profile_id}")])
    if item.enabled:
        buttons.append([Button.inline("🧪 测试发送", f"dp:t:{item.profile_id}")])
    buttons.append([Button.inline("/profiles", "dp:r"), Button.inline("/start", "h:r")])
    return text, buttons


def destination_profile_test_confirm_view(item: DestinationProfileView, token: int) -> tuple[str, list]:
    text = "\n".join(
        [
            f"⚠️ 测试目的地：{item.name}",
            "将向目标发送 1 条短测试消息，然后立即删除。",
            "这是外部副作用；只有本次确认后才执行。",
        ]
    )
    return text, [
        [Button.inline("✅ 确认测试", f"dp:tc:{token}"), Button.inline("取消", f"dp:v:{item.profile_id}")]
    ]
