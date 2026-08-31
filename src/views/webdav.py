"""WebDAV configuration renderers."""

from dataclasses import dataclass
from datetime import datetime

from telethon import Button


@dataclass(frozen=True)
class WebDavConfigViewState:
    enabled: bool
    url: str = ""
    user: str = ""
    has_password: bool = False
    path: str = ""
    retry: int | str | None = 0
    backup_policy: str = "best_effort"


@dataclass(frozen=True)
class BackupAttemptListItemView:
    attempt_id: int
    legacy_seq: int | None
    state: str
    remote_dir: str
    total_files: int
    succeeded_files: int
    failed_files: int
    total_bytes: int
    created_at: float
    error_code: str = ""


@dataclass(frozen=True)
class BackupAttemptPageView:
    page: int
    pages: int
    total: int
    items: tuple[BackupAttemptListItemView, ...]


@dataclass(frozen=True)
class BackupFileItemView:
    file_id: int
    remote_name: str
    size_bytes: int
    state: str
    bytes_done: int
    error_code: str = ""


@dataclass(frozen=True)
class BackupAttemptDetailView:
    attempt_id: int
    legacy_seq: int | None
    state: str
    remote_dir: str
    retry_count: int
    next_retry_at: float | None
    error_code: str
    page: int
    pages: int
    total: int
    files: tuple[BackupFileItemView, ...]


def webdav_cfg_lines(state: WebDavConfigViewState) -> list:
    val = {
        "url": state.url or "（未设置）",
        "user": state.user or "（未设置）",
        "pass": "***" if state.has_password else "（未设置）",
        "path": state.path or "（未设置）",
        "retry": f"{state.retry} 次",
        "policy": "required（备份成功才最终完成）" if state.backup_policy == "required" else "best_effort（备份失败不挡发布）",
    }
    return [
        f"🔗 地址    {val['url']}",
        f"👤 账号    {val['user']}",
        f"🔑 密码    {val['pass']}",
        f"📂 路径    {val['path']}",
        f"🔄 重试    {val['retry']}",
        f"🛡 策略    {val['policy']}",
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
        [Button.inline("📁 上传记录", "wd_cfg:logs")],
        [Button.inline("🛡 备份策略", "wd_cfg:policy")],
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


def webdav_required_policy_confirm_view(operation_id: int) -> tuple[str, list]:
    text = (
        "⚠️ 启用 required 备份策略\n"
        "────────────────────────\n"
        "Telegram 发布成功后，任务仍会等待 WebDAV 备份。\n"
        "如果备份最终失败，任务会标记失败并保留本地缓存；已经发布的 Telegram 消息不会自动撤回。\n\n"
        "这可能让任务长时间处于未完成状态。确认启用吗？"
    )
    return text, [
        [
            Button.inline("✅ 确认启用", f"wd_bp:y:{int(operation_id)}"),
            Button.inline("❌ 取消", f"wd_bp:n:{int(operation_id)}"),
        ],
        [Button.inline("⬅️ 返回 WebDAV", "wd_cfg:back")],
    ]


def _human_size(value: int) -> str:
    size = max(0, int(value))
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _short(value: str, limit: int = 72) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def backup_attempt_page_view(state: BackupAttemptPageView) -> tuple[str, list]:
    page = min(max(0, state.page), max(1, state.pages) - 1)
    lines = [
        f"☁️ WebDAV 上传记录 · 第 {page + 1}/{max(1, state.pages)} 页",
        f"共 {state.total} 次 durable attempt",
        "──────────",
    ]
    buttons: list = []
    if not state.items:
        lines.append("暂无 durable WebDAV 上传记录。")
    for index, item in enumerate(state.items, start=1):
        status = {
            "pending": "⏳ 等待",
            "running": "📤 上传中",
            "retry_wait": "⏰ 等待重试",
            "succeeded": "✅ 成功",
            "failed": "❌ 失败",
            "interrupted": "⚠️ 中断",
            "deleted": "🗑 已删除",
        }.get(item.state, item.state)
        seq = item.legacy_seq if item.legacy_seq is not None else item.attempt_id
        created = datetime.fromtimestamp(item.created_at).strftime("%m-%d %H:%M")
        lines.append(
            f"{index}. {status} · job #{seq} · {created}\n"
            f"   文件 {item.succeeded_files}/{item.total_files} · {_human_size(item.total_bytes)}"
        )
        if item.failed_files:
            lines.append(f"   失败 {item.failed_files} · {item.error_code or 'unknown'}")
        buttons.append([Button.inline(f"{index} 详情", f"wd:a:{item.attempt_id}:0")])
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️", f"wd:p:{page - 1}"))
    nav.append(Button.inline("🔄", f"wd:p:{page}"))
    if page + 1 < max(1, state.pages):
        nav.append(Button.inline("➡️", f"wd:p:{page + 1}"))
    buttons.append(nav)
    buttons.append([Button.inline("⬅️ WebDAV", "wd_cfg:back"), Button.inline("🏠 首页", "h:r")])
    return "\n".join(lines), buttons


def backup_attempt_detail_view(state: BackupAttemptDetailView) -> tuple[str, list]:
    seq = state.legacy_seq if state.legacy_seq is not None else state.attempt_id
    lines = [
        f"☁️ WebDAV Attempt #{state.attempt_id}",
        f"任务：#{seq}",
        f"状态：{state.state}",
        f"远端目录：{_short(state.remote_dir, 96) or '（空）'}",
        f"重试：{state.retry_count}",
        f"文件：{state.total}",
        "──────────",
    ]
    if state.error_code:
        lines.append(f"错误码：{state.error_code}")
    if state.next_retry_at:
        lines.append(f"下次自动重试：{datetime.fromtimestamp(state.next_retry_at).strftime('%m-%d %H:%M:%S')}")
    buttons: list = []
    for index, item in enumerate(state.files, start=1):
        mark = "✅" if item.state in {"succeeded", "deleted"} else "❌" if item.state == "failed" else "⏳"
        lines.append(
            f"{index}. {mark} {_short(item.remote_name)} · {_human_size(item.size_bytes)} · {item.state}"
            + (f" · {item.error_code}" if item.error_code else "")
        )
        if item.state not in {"succeeded", "deleted"}:
            buttons.append([Button.inline(f"🔄 重试文件 {index}", f"wd:fr:{item.file_id}")])
    if any(item.state not in {"succeeded", "deleted"} for item in state.files):
        buttons.append([Button.inline("🔄 重试本 attempt 失败文件", f"wd:ar:{state.attempt_id}")])
    if state.total > 0 and state.state != "deleted":
        buttons.append([Button.inline("🗑 删除本 attempt 远端文件", f"wd:del:{state.attempt_id}")])
    page = min(max(0, state.page), max(1, state.pages) - 1)
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️", f"wd:a:{state.attempt_id}:{page - 1}"))
    if page + 1 < max(1, state.pages):
        nav.append(Button.inline("➡️", f"wd:a:{state.attempt_id}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline("⬅️ 上传记录", "wd:p:0"), Button.inline("🏠 首页", "h:r")])
    return "\n".join(lines), buttons


def webdav_delete_confirm_view(operation_id: int, *, remote_dir: str, file_count: int, total_bytes: int) -> tuple[str, list]:
    text = (
        "⚠️ 确认删除 WebDAV 远端文件\n"
        "────────────────────────\n"
        f"目录：{_short(remote_dir, 96)}\n"
        f"文件：{int(file_count)} 个\n"
        f"总大小：{_human_size(total_bytes)}\n\n"
        "只会逐个删除数据库记录的具体文件，不会递归删除目录。"
    )
    return text, [
        [
            Button.inline("🗑 确认删除", f"wd_dr:y:{int(operation_id)}"),
            Button.inline("❌ 取消", f"wd_dr:n:{int(operation_id)}"),
        ],
        [Button.inline("⬅️ 返回记录", "wd:p:0")],
    ]
