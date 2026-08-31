"""Proxy settings renderers using already-redacted labels."""

from dataclasses import dataclass

from telethon import Button


@dataclass(frozen=True)
class ProxyViewState:
    current: int
    auto: bool
    labels: tuple[str, ...] = ()
    current_label: str = "直连"


def proxy_view(state: ProxyViewState) -> tuple:
    if state.current >= 0:
        conn = f"代理 #{state.current + 1}：{state.current_label}"
    else:
        conn = "直连"
    auto = "✅ 已开启" if state.auto else "⛔ 已关闭"
    lines = [
        "🌐 代理设置（仅 HTTP 代理）",
        "",
        f"当前连接：{conn}",
        f"自动切换：{auto}",
        f"代理数量：{len(state.labels)}",
        "",
        "下载网络失败时自动切换到下一个可用代理。",
    ]
    buttons = [
        [Button.inline("➕ 添加代理", "proxy:add")],
        [
            Button.inline(
                "⛔ 关闭自动切换" if state.auto else "🔛 开启自动切换",
                "proxy:auto",
            )
        ],
        [
            Button.inline("🔀 管理代理", "proxy:list"),
            Button.inline("🔌 直连", "proxy:direct"),
        ],
        [Button.inline("/start", "h:r")],
    ]
    return "\n".join(lines), buttons


def proxy_list_view(state: ProxyViewState) -> tuple:
    lines = ["🔀 代理列表", ""]
    buttons = [
        [Button.inline("⬅️ 返回", "proxy:back")],
        [Button.inline("/start", "h:r")],
    ]
    if not state.labels:
        lines.append("（暂无代理，点 ➕ 添加）")
    for idx, label in enumerate(state.labels):
        mark = "✅ " if idx == state.current else ""
        lines.append(f"{mark}代理 #{idx + 1}：{label}")
        buttons.append(
            [
                Button.inline("✅ 使用", f"proxy:use:{idx}"),
                Button.inline("🧪 测试", f"proxy:test:{idx}"),
                Button.inline("🗑 删除", f"proxy:del:{idx}"),
            ]
        )
    return "\n".join(lines), buttons
