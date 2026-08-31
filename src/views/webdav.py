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
        [Button.inline("🧪 测试连接", "wd_cfg:test")],
        [Button.inline("⚙️ 修改配置", "wd_cfg:edit")],
        [Button.inline("🏠 首页", "h:r")],
    ]
    return "\n".join(lines), buttons


def webdav_probe_view(result) -> tuple[str, list]:
    if result.ok:
        lines = [
            "🧪 WebDAV 连接测试",
            "────────────────────────",
            "✅ 路径可读取",
            f"HTTP：{result.status}",
        ]
        if result.quota_supported:
            if result.quota_used_bytes is not None:
                lines.append(f"已使用：{_fmt_bytes(result.quota_used_bytes)}")
            if result.quota_available_bytes is not None:
                lines.append(f"可用额度：{_fmt_bytes(result.quota_available_bytes)}")
        else:
            lines.append("容量：服务器未提供 DAV quota")
    else:
        lines = [
            "🧪 WebDAV 连接测试",
            "────────────────────────",
            "❌ 连接/读取检查失败",
            f"HTTP：{result.status if result.status is not None else '无响应'}",
            f"原因：{result.message}",
        ]
    lines.extend(
        [
            "────────────────────────",
            "此测试只执行只读 PROPFIND，不会上传、删除或创建远端文件。",
        ]
    )
    buttons = [
        [Button.inline("🔄 再测一次", "wd_cfg:test")],
    ]
    if result.ok:
        buttons.append([Button.inline("✍️ 写入测试", "wd_cfg:wtest")])
    buttons.extend([
        [Button.inline("⬅️ 返回 WebDAV", "wd_cfg:back")],
        [Button.inline("🏠 首页", "h:r")],
    ])
    return "\n".join(lines), buttons


def webdav_write_confirm_view(operation_id: int) -> tuple[str, list]:
    return (
        "✍️ WebDAV 写入测试确认\n"
        "────────────────────────\n"
        "将创建一个随机 .tgvf-check-* 小文件，完成 PUT 后校验远端大小，再立即 DELETE。\n"
        "不会覆盖已有文件，也不会递归删除目录。\n"
        "只有确认后才会产生远端写入副作用。",
        [
            [Button.inline("✅ 确认写入测试", f"wd_w:y:{int(operation_id)}")],
            [Button.inline("❌ 取消", f"wd_w:n:{int(operation_id)}")],
            [Button.inline("⬅️ 返回 WebDAV", "wd_cfg:back")],
        ],
    )


def webdav_write_result_view(result) -> tuple[str, list]:
    icon = "✅" if result.ok else "⚠️"
    lines = [
        "✍️ WebDAV 写入测试",
        "────────────────────────",
        f"{icon} {result.status_message}",
        f"PUT：{'成功' if result.uploaded else '未确认'}",
        f"远端大小校验：{'成功' if result.verified else '未确认'}",
        f"测试文件清理：{'成功' if result.cleaned else '失败'}",
    ]
    if not result.cleaned:
        lines.append("⚠️ 请检查远端是否残留 .tgvf-check-* 测试文件。")
    return "\n".join(lines), [
        [Button.inline("⬅️ 返回 WebDAV", "wd_cfg:back")],
        [Button.inline("🏠 首页", "h:r")],
    ]


def _fmt_bytes(value: int) -> str:
    size = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


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
