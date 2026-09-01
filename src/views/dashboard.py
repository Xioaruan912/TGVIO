"""Private Telegram view for opening the read-only Dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from telethon import Button

from .home import home_button


@dataclass(frozen=True)
class DashboardAccessViewState:
    enabled: bool
    public_url: str
    token: str


def dashboard_access_view(state: DashboardAccessViewState) -> tuple[str, list]:
    """Render credentials only when the complete static configuration is ready."""
    if not state.enabled:
        return (
            "🔐 Dashboard\n──────────\n当前未启用。请先在服务器静态配置中启用。",
            [home_button()],
        )
    if not state.public_url or not state.token:
        return (
            "🔐 Dashboard\n──────────\n访问凭据配置不完整，请检查服务器静态配置并重启。",
            [home_button()],
        )

    public_url = escape(state.public_url, quote=False)
    token = escape(state.token, quote=False)
    text = (
        "🔐 Dashboard 访问凭据\n"
        "──────────\n"
        "状态：已启用 · 只读\n"
        f"地址：<code>{public_url}</code>\n\n"
        "Bearer Token（点击或长按复制）：\n"
        f"<code>{token}</code>\n\n"
        "⚠️ 仅供本人使用，请勿转发此消息或 Token。"
    )
    return text, [[Button.url("🌐 打开 Dashboard", state.public_url)], home_button()]
