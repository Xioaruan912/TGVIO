"""Pure F4 runtime status renderer."""

from __future__ import annotations

from telethon import Button

from ..services.stats import RuntimeStatsSnapshot


def _bytes(value: int | None) -> str:
    if value is None:
        return "未知"
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


def _uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分钟"


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "无缓存探测"
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{int(seconds // 3600)} 小时前"


def stats_view(state: RuntimeStatsSnapshot) -> tuple[str, list]:
    cpu = "采样中" if state.cpu_percent is None else f"{state.cpu_percent:.1f}%"
    if state.disk_used_bytes is None or state.disk_total_bytes is None:
        disk = "未知"
    else:
        disk = f"{_bytes(state.disk_used_bytes)} / {_bytes(state.disk_total_bytes)}"
    disk_state = "安全" if state.disk_healthy else "需关注" if state.disk_healthy is False else "未知"
    webdav = "未启用" if not state.webdav_enabled else f"{state.webdav_health}（{_age(state.webdav_age_seconds)}）"
    errors = "、".join(f"{code}×{count}" for code, count in state.recent_errors[:3]) or "无"
    events = "、".join(f"{name}×{count}" for name, count in state.recent_events[:3]) or "无"
    text = "\n".join(
        [
            "📊 运行状态",
            "──────────",
            f"运行时间：{_uptime(state.uptime_seconds)}",
            f"今日任务：{state.today_succeeded} 完成 · {state.today_failed} 失败 · {state.today_cancelled} 取消",
            f"累计发布：{_bytes(state.total_published_bytes)}",
            f"累计备份：{_bytes(state.total_backed_up_bytes)}",
            f"秒传节省：{_bytes(state.saved_upload_bytes)}",
            f"当前队列：{state.running} 运行 · {state.waiting} 等待 · {state.failed} 失败",
            "──────────",
            f"CPU：{cpu} · 内存：{state.memory_mb:.0f} MB",
            f"磁盘：{disk}（{disk_state} · {'强制' if state.disk_enforce else '监控'}）",
            f"缓存：预留 {_bytes(state.disk_reserved_bytes)} · 保护 {_bytes(state.protected_cache_bytes)} · 可清 {_bytes(state.reclaimable_cache_bytes)}",
            f"Telegram：{'正常' if state.telegram_connected else '未连接'}",
            f"WebDAV：{webdav}",
            f"队列数据库：{'正常' if state.database_ok else '异常'}",
            f"最近错误：{errors}",
            f"最近事件：{events}",
        ]
    )
    buttons = [
        [Button.inline("🔄 刷新", "h:status"), Button.inline("🩺 健康检查", "h:health")],
        [Button.inline("🧾 导出诊断", "h:diag")],
        [Button.inline("🏠 首页", "h:r")],
    ]
    return text, buttons
