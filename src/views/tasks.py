"""U2 durable queue pages, job details, failure center and confirmations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from telethon import Button

from ..progress import render_bar
from .home import home_button


FILTER_LABELS = {
    "all": "全部",
    "running": "运行中",
    "waiting": "等待",
    "paused": "暂停",
    "failed": "失败",
    "completed": "已完成",
}


@dataclass(frozen=True)
class DurableQueueItemView:
    job_id: int
    legacy_seq: int | None
    state: str
    pct: int | None = None
    total_items: int = 0


@dataclass(frozen=True)
class DurableQueuePageView:
    filter_name: str
    page: int
    pages: int
    total: int
    running: int
    waiting: int
    paused: int
    failed: int
    items: tuple[DurableQueueItemView, ...] = ()


@dataclass(frozen=True)
class JobDetailViewState:
    job_id: int
    legacy_seq: int | None
    revision: int
    kind: str
    state: str
    source_kind: str
    item_count: int
    item_bytes: int
    bytes_done: int
    bytes_total: int
    retry_count: int
    cache_exists: bool
    published_count: int
    backup_state: str
    backup_summary: str = ""
    error_message: str = ""
    error_code: str = ""
    next_retry_at: float | None = None
    accepted_at: float = 0.0
    can_retry: bool = False
    media_compat_summary: str = ""
    destination_profile: str = ""


@dataclass(frozen=True)
class FailureItemView:
    job_id: int
    legacy_seq: int | None
    revision: int
    cache_exists: bool
    error_message: str
    can_retry: bool
    error_code: str = ""
    next_retry_at: float | None = None


ERROR_ACTIONS = {
    "telegram_auth": "重新登录并检查 session",
    "telegram_permission": "检查目标频道权限",
    "source_expired": "重新转发源消息",
    "url_unsupported": "更新 yt-dlp 或更换来源",
    "file_too_large": "压缩文件或调整发布上限",
    "disk_low": "清理缓存或扩容",
    "cache_missing": "重新下载源文件",
    "media_invalid": "检查媒体或执行兼容性处理",
    "publish_partial": "检查已发布消息；需要时先撤销再重新处理",
    "webdav_auth": "检查 WebDAV 账号密码",
    "webdav_not_found": "检查 WebDAV 远端路径",
    "webdav_locked": "等待文件解锁后重试",
    "webdav_server": "等待 WebDAV 服务恢复",
    "network_timeout": "检查网络或代理后重试",
    "network_unreachable": "检查网络或切换代理",
    "telegram_flood_wait": "等待 Telegram 限流解除",
    "unknown": "重试；持续失败请导出诊断",
}


def _state_text(state: str) -> tuple[str, str]:
    mapping = {
        "collecting": ("📥", "收集中"),
        "awaiting_confirmation": ("⏳", "待确认"),
        "queued": ("⏳", "等待下载"),
        "downloading": ("⬇️", "下载中"),
        "ready": ("⏳", "等待发布"),
        "publishing": ("📤", "发布中"),
        "paused": ("⏸", "已暂停"),
        "interrupted": ("⚠️", "等待恢复"),
        "failed": ("❌", "失败"),
        "succeeded": ("✅", "已完成"),
        "cancelled": ("⚠️", "已取消"),
    }
    return mapping.get(state, ("•", state))


def durable_queue_view(state: DurableQueuePageView) -> tuple[str, list]:
    pages = max(1, state.pages)
    page = min(max(0, state.page), pages - 1)
    lines = [
        f"📋 任务队列 · 第 {page + 1}/{pages} 页",
        f"筛选：{FILTER_LABELS.get(state.filter_name, '全部')}",
        "──────────",
    ]
    buttons = []
    if not state.items:
        lines.append("当前筛选下没有任务。")
    for index, item in enumerate(state.items, start=1):
        emoji, label = _state_text(item.state)
        seq = item.legacy_seq if item.legacy_seq is not None else item.job_id
        suffix = ""
        if item.pct is not None and item.state in {"downloading", "publishing"}:
            suffix = f" · {item.pct}%"
        elif item.total_items > 1:
            suffix = f" · {item.total_items} 个媒体"
        lines.append(f"{index}. {emoji} #{seq} {label}{suffix}")
        buttons.append([Button.inline(f"{index} 详情", f"j:v:{item.job_id}")])
    lines.extend(
        [
            "──────────",
            f"运行 {state.running} · 等待 {state.waiting} · 暂停 {state.paused} · 失败 {state.failed}",
        ]
    )
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️", f"q:p:{state.filter_name}:{page - 1}"))
    nav.append(Button.inline("🔄 刷新", f"q:p:{state.filter_name}:{page}"))
    if page + 1 < pages:
        nav.append(Button.inline("➡️", f"q:p:{state.filter_name}:{page + 1}"))
    buttons.append(nav)
    filter_order = ["all", "running", "waiting", "paused", "failed", "completed"]
    current = filter_order.index(state.filter_name) if state.filter_name in filter_order else 0
    next_filter = filter_order[(current + 1) % len(filter_order)]
    buttons.append(
        [
            Button.inline(
                f"筛选：{FILTER_LABELS.get(state.filter_name, '全部')}",
                f"q:f:{next_filter}:0",
            ),
            Button.inline("❌ 失败中心", "q:f:failed:0"),
        ]
    )
    buttons.append([Button.inline("🧰 批量操作", "q:b")])
    buttons.append(home_button())
    return "\n".join(lines), buttons


def job_detail_view(state: JobDetailViewState) -> tuple[str, list]:
    emoji, label = _state_text(state.state)
    seq = state.legacy_seq if state.legacy_seq is not None else state.job_id
    size = f"{state.item_bytes / 1024 / 1024:.1f} MB" if state.item_bytes else "未知"
    source = "URL" if state.source_kind == "url" else "Telegram"
    accepted = datetime.fromtimestamp(state.accepted_at).strftime("%m-%d %H:%M") if state.accepted_at else "未知"
    lines = [
        f"{emoji} 任务 #{seq} · {label}",
        "──────────",
        f"🆔 DB job：{state.job_id} · revision {state.revision}",
        f"📥 来源：{source}",
        f"🎬 媒体：{state.item_count} 个 · {size}",
        f"🕒 创建：{accepted}",
        f"🔁 重试：{state.retry_count} 次",
        f"💾 本地缓存：{'存在' if state.cache_exists else '无'}",
        f"📢 已发布消息：{state.published_count}",
        f"☁️ 备份：{state.backup_state or 'disabled'}{(' · ' + state.backup_summary) if state.backup_summary else ''}",
    ]
    if state.destination_profile:
        lines.insert(4, f"🎯 目的地：{state.destination_profile[:80]}")
    if state.media_compat_summary:
        lines.append(f"🎞 兼容性：{state.media_compat_summary}")
    if state.bytes_total > 0 and state.state in {"downloading", "publishing"}:
        pct = max(0, min(100, int(state.bytes_done * 100 / state.bytes_total)))
        lines.append(f"{render_bar(pct)} {pct}%")
    if state.error_message:
        lines.append(f"🧾 原因：{state.error_message[:300]}")
    if state.error_code:
        lines.append(f"🏷 错误码：{state.error_code}")
        action = ERROR_ACTIONS.get(state.error_code)
        if action:
            lines.append(f"💡 建议：{action}")
    if state.next_retry_at and state.next_retry_at > datetime.now().timestamp():
        retry_at = datetime.fromtimestamp(state.next_retry_at).strftime("%m-%d %H:%M:%S")
        lines.append(f"⏰ 预计重试：{retry_at}")
    buttons = []
    if state.state not in {"succeeded", "cancelled", "failed"}:
        buttons.append([Button.inline("⚠️ 取消任务", f"j:c:{state.job_id}:{state.revision}")])
    if state.state == "failed" and state.can_retry:
        buttons.append([Button.inline("🔄 重试", f"j:r:{state.job_id}:{state.revision}")])
    if state.state == "failed" and state.cache_exists:
        buttons.append([Button.inline("🗑 删除缓存", f"j:d:{state.job_id}:{state.revision}")])
    if state.published_count:
        buttons.append([Button.inline("↩️ 撤销发布", f"j:u:{state.job_id}:{state.revision}")])
    buttons.append([Button.inline("📋 返回队列", "q:p:all:0"), home_button()[0]])
    return "\n".join(lines), buttons


def failure_center_view(items: tuple[FailureItemView, ...], *, page: int, pages: int) -> tuple[str, list]:
    pages = max(1, pages)
    lines = [f"❌ 失败中心 · 第 {page + 1}/{pages} 页", "──────────"]
    buttons = []
    if not items:
        lines.append("当前没有需要处理的失败任务。")
    for index, item in enumerate(items, start=1):
        seq = item.legacy_seq if item.legacy_seq is not None else item.job_id
        cache = "缓存可用" if item.cache_exists else "无缓存"
        reason = item.error_message[:80] if item.error_message else "需要检查任务详情"
        code = f" [{item.error_code}]" if item.error_code else ""
        lines.append(f"{index}. ❌ #{seq} · {cache}{code}\n   {reason}")
        buttons.append([Button.inline(f"{index} 查看处理", f"j:v:{item.job_id}")])
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️", f"q:f:failed:{page - 1}"))
    nav.append(Button.inline("🔄 刷新", f"q:f:failed:{page}"))
    if page + 1 < pages:
        nav.append(Button.inline("➡️", f"q:f:failed:{page + 1}"))
    buttons.append(nav)
    buttons.append([Button.inline("📋 全部队列", "q:p:all:0"), home_button()[0]])
    return "\n".join(lines), buttons


def confirmation_view(*, operation_id: int, action: str, label: str) -> tuple[str, list]:
    action_name = {
        "cancel": "取消任务",
        "delete_cache": "删除本地缓存",
        "undo": "撤销已发布消息",
        "batch_cancel": "取消全部等待任务",
        "batch_delete_cache": "清理全部失败缓存",
    }.get(action, "执行操作")
    text = "\n".join(
        [
            "⚠️ 请确认操作",
            "──────────",
            f"任务：{label}",
            f"操作：{action_name}",
            "此确认将在 5 分钟后失效。",
        ]
    )
    buttons = [
        [Button.inline("✅ 确认", f"x:y:{operation_id}"), Button.inline("❌ 取消", f"x:n:{operation_id}")],
        home_button(),
    ]
    return text, buttons


def batch_actions_view(*, waiting_count: int, failed_count: int, failed_bytes: int) -> tuple[str, list]:
    cache = f"{failed_bytes / 1024 / 1024:.1f} MB" if failed_bytes else "0 MB"
    text = "\n".join(
        [
            "🧰 批量操作",
            "──────────",
            f"⏳ 等待任务：{waiting_count}",
            f"❌ 失败任务：{failed_count}",
            f"💾 失败缓存：{cache}",
            "所有 destructive 操作都会再次确认，并逐任务执行。",
        ]
    )
    buttons = [
        [Button.inline("⚠️ 取消全部等待任务", "q:bc")],
        [Button.inline("🗑 清理全部失败缓存", "q:bd")],
        [Button.inline("📋 返回队列", "q:p:all:0"), home_button()[0]],
    ]
    return text, buttons
