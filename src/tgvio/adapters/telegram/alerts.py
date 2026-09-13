from __future__ import annotations

import logging
from typing import Awaitable, Callable

from tgvio.application.notifications import WebhookDeliveryError
from tgvio.observability import log_event


_MAX_ALERT_CHARS = 1800

_ALERT_HEADLINES = {
    "job.failed": "⚠️ TGVIO 任务失败",
    "archive.failed": "☁️ TGVIO 归档失败",
    "runtime.telegram_disconnected": "🔌 Telegram 连接断开",
    "runtime.telegram_recovered": "✅ Telegram 连接已恢复",
    "runtime.disk_low": "💾 磁盘空间不足",
    "runtime.disk_recovered": "✅ 磁盘空间已恢复",
}


def render_alert(payload: dict[str, object]) -> str:
    """Render a fixed, redacted owner alert. No captions/paths/URLs/credentials."""

    event = str(payload.get("event", ""))
    lines = [_ALERT_HEADLINES.get(event, "⚠️ TGVIO 告警"), ""]
    accepted_order = payload.get("accepted_order")
    if isinstance(accepted_order, int):
        lines.append(f"任务：#{accepted_order}")
    job_id = payload.get("job_id")
    if isinstance(job_id, str) and job_id and not isinstance(accepted_order, int):
        lines.append(f"任务：{job_id[:8]}…")
    package_id = payload.get("package_id")
    if isinstance(package_id, str) and package_id:
        lines.append(f"归档包：{package_id[:8]}…")
    state = payload.get("state")
    if isinstance(state, str) and state:
        lines.append(f"状态：{state}")
    error_code = payload.get("error_code")
    if isinstance(error_code, str) and error_code:
        lines.append(f"错误码：{error_code}")
    component = payload.get("component")
    if isinstance(component, str) and component:
        lines.append(f"组件：{component}")
    free_bytes = payload.get("free_bytes")
    reserve_bytes = payload.get("reserve_bytes")
    if isinstance(free_bytes, int):
        lines.append(f"可用：{_human_bytes(free_bytes)}")
    if isinstance(reserve_bytes, int):
        lines.append(f"预留：{_human_bytes(reserve_bytes)}")
    lines.append("")
    lines.append("详情见 /jobs 与 /diag。")
    text = "\n".join(lines)
    return text[:_MAX_ALERT_CHARS]


def _human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


class TelegramOwnerNotifier:
    """Deliver one redacted alert to the owner's private Telegram chat."""

    def __init__(
        self,
        sender: Callable[[str], Awaitable[object]],
        *,
        log_name: str = "tgvio.alerts",
    ) -> None:
        self._sender = sender
        self._log = logging.getLogger(log_name)

    async def deliver(self, payload: dict[str, object]) -> None:
        text = render_alert(payload)
        try:
            await self._sender(text)
        except Exception as exc:  # noqa: BLE001 - normalized to a safe code
            log_event(
                self._log,
                logging.WARNING,
                "alert.telegram.failed",
                alert_event=str(payload.get("event", "")),
                exception_type=type(exc).__name__,
            )
            raise WebhookDeliveryError("telegram_alert_failed", type(exc).__name__) from exc
