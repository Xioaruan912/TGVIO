"""Single source of truth for user-facing Telegram commands.

Keep command names, BotFather descriptions and human help text synchronized.
Context-specific confirmation buttons may still exist, but top-level navigation
should point users back to these slash commands.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    name: str
    menu_description: str
    help_text: str
    group: str


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("start", "首页与常用命令", "查看状态和常用命令", "基础"),
    CommandSpec("queue", "查看任务和失败项", "查看运行、等待、失败任务", "任务"),
    CommandSpec("begin", "开始合集", "开始收集多条媒体和文字", "任务"),
    CommandSpec("end", "结束并发布合集", "结束当前合集并按顺序发布", "任务"),
    CommandSpec("mode", "设置 18+ 处理", "切换正常、询问或雪花遮挡", "任务"),
    CommandSpec("profiles", "管理发布目标", "管理发布频道和发布策略", "配置"),
    CommandSpec("webdav", "配置 WebDAV 备份", "配置、测试和开关 WebDAV", "配置"),
    CommandSpec("webdavlogs", "查看备份记录", "查看 WebDAV 记录和失败项", "配置"),
    CommandSpec("proxy", "管理下载代理", "管理 URL 下载使用的 HTTP 代理", "配置"),
    CommandSpec("dashboard", "获取控制台凭据", "获取只读控制台地址和 Token", "系统"),
    CommandSpec("stats", "查看运行统计", "查看任务、资源和缓存统计", "系统"),
    CommandSpec("health", "检查本地健康", "检查 heartbeat、数据库和磁盘", "系统"),
    CommandSpec("diag", "导出脱敏诊断", "查看不含凭证和内容的诊断摘要", "系统"),
    CommandSpec("about", "查看全部命令", "查看完整命令说明", "系统"),
)


def command_help_text(*, include_start: bool = True) -> str:
    """Render the complete command reference in compact grouped form."""
    groups: list[str] = []
    for group_name in ("基础", "任务", "配置", "系统"):
        specs = [
            spec
            for spec in COMMAND_SPECS
            if spec.group == group_name and (include_start or spec.name != "start")
        ]
        if not specs:
            continue
        lines = [group_name]
        lines.extend(f"/{spec.name} — {spec.help_text}" for spec in specs)
        groups.append("\n".join(lines))
    return "\n\n".join(groups)


def command_menu_pairs() -> tuple[tuple[str, str], ...]:
    """Return Telegram BotCommand-compatible ``(name, description)`` pairs."""
    return tuple((spec.name, spec.menu_description) for spec in COMMAND_SPECS)
