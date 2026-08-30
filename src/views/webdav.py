"""WebDAV configuration renderers."""

from dataclasses import dataclass

from telethon import Button


@dataclass(frozen=True)
class WebDavConfigViewState:
    enabled: bool
    url: str = ""
    user: str = ""
    has_password: bool = False
    path: str = ""
    retry: int | str | None = 0


def webdav_cfg_lines(state: WebDavConfigViewState) -> list:
    val = {
        "url": state.url or "（未设置）",
        "user": state.user or "（未设置）",
        "pass": "***" if state.has_password else "（未设置）",
        "path": state.path or "（未设置）",
        "retry": f"{state.retry} 次",
    }
    return [
        f"🔗 地址    {val['url']}",
        f"👤 账号    {val['user']}",
        f"🔑 密码    {val['pass']}",
        f"📂 路径    {val['path']}",
        f"🔄 重试    {val['retry']}",
    ]


def webdav_cfg_view(state: WebDavConfigViewState) -> tuple:
    status = "✅ 已启用" if state.enabled else "⛔ 已停用"
    lines = [
        "📁 WebDAV 备份配置",
        "下载完成后自动备份媒体",
        "────────────────────────",
        f"状态    {status}",
        *webdav_cfg_lines(state),
        "────────────────────────",
    ]
    toggle = (
        Button.inline("⛔ 停用", "wd_cfg:off")
        if state.enabled
        else Button.inline("🔛 启用", "wd_cfg:on")
    )
    buttons = [
        [toggle],
        [Button.inline("⚙️ 修改配置", "wd_cfg:edit")],
        [Button.inline("🏠 首页", "h:r")],
    ]
    return "\n".join(lines), buttons


def webdav_cfg_fields_view(state: WebDavConfigViewState) -> tuple:
    lines = [
        "⚙️ WebDAV 修改配置",
        "────────────────────────",
        *webdav_cfg_lines(state),
        "────────────────────────",
        "点击按钮，直接回复新值即可：",
    ]
    buttons = [
        [
            Button.inline("✏️ 地址", "wd_cfg:url"),
            Button.inline("✏️ 账号", "wd_cfg:user"),
        ],
        [
            Button.inline("✏️ 密码", "wd_cfg:pass"),
            Button.inline("✏️ 路径", "wd_cfg:path"),
        ],
        [Button.inline("✏️ 重试", "wd_cfg:retry")],
        [Button.inline("⬅️ 返回", "wd_cfg:back")],
        [Button.inline("🏠 首页", "h:r")],
    ]
    return "\n".join(lines), buttons
