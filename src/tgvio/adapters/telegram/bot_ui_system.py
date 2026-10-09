from __future__ import annotations

from datetime import datetime
import logging
import shutil
from zoneinfo import ZoneInfo

from telethon import Button

from tgvio.application.operation_tokens import OperationTokenInvalidError
from tgvio.domain.job import JobState
from tgvio.observability import log_event


_PROBLEM_LABELS = {
    "maintenance.daily.failed": "每日整理没有完成",
    "maintenance.runtime.failed": "后台整理出错",
    "auto_recovery.disk_cleanup.failed": "自动清理空间失败",
    "auto_recovery.job.exhausted": "有任务自动重试多次仍失败",
    "auto_recovery.job.quarantined": "有任务可能已发出一部分，已停止",
    "download.job.failed": "有任务下载失败",
    "job.run.failed": "有任务处理出错",
    "archive.package.failed": "云端备份出错",
    "archive.execution.failed": "云端备份出错",
    "archive.object.failed": "云端备份出错",
}


def problem_label(row: dict[str, object]) -> str:
    event = str(row.get("event") or "")
    if event in _PROBLEM_LABELS:
        return _PROBLEM_LABELS[event]
    if str(row.get("component") or "").startswith("telethon"):
        return "按钮或消息处理出错"
    if event.startswith("publish."):
        return "发到频道时出错"
    return "程序内部出错"


class BotUISystemMixin:
    """Runtime status, statistics, health and local cache pages."""

    async def _run_cache_cleanup_callback(
        self,
        event,
        *,
        owner_id: int | None = None,
        token: str | None = None,
    ) -> None:
        if self._cache_operator is None:
            await self._edit_page(event, "缓存维护服务未启用。", self._nav_buttons())
            return
        cleanup_targets: tuple[str, ...] | None = None
        if self._operation_tokens is not None:
            if owner_id is None or not token:
                await self._safe_answer(event, "确认操作已过期，请重新打开缓存页面", alert=True)
                return
            try:
                operation = await self._operation_tokens.inspect(
                    token=token,
                    owner_id=owner_id,
                    action="cache_cleanup",
                )
                if operation.resource_type != "cache" or operation.resource_id != "managed":
                    raise OperationTokenInvalidError("invalid cache resource")
                cleanup_targets = await self._cache_operator.cleanup_candidates(force=True)
                payload = {
                    "version": 1,
                    "scope": "managed_terminal_cache",
                    "job_ids": list(cleanup_targets),
                }
                await self._operation_tokens.consume(
                    token=token,
                    owner_id=owner_id,
                    action="cache_cleanup",
                    resource_type="cache",
                    resource_id="managed",
                    expected_revision=len(cleanup_targets),
                    payload=payload,
                )
            except OperationTokenInvalidError:
                await self._safe_answer(event, "确认操作已过期，请重新打开缓存页面", alert=True)
                return
        try:
            result = await self._cache_operator.cleanup(
                force=True,
                job_ids=cleanup_targets if self._operation_tokens is not None else None,
            )
        except Exception as exc:
            log_event(
                self._log,
                logging.ERROR,
                "telegram.cache.cleanup_failed",
                "Cache cleanup requested from Telegram failed",
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            await self._edit_page(
                event,
                "❌ 缓存清理没有完成。没有删除失败任务或归档未完成任务的数据，请稍后重试。",
                self._cache_buttons(),
            )
            return
        await self._edit_page(
            event,
            (
                f"🧹 已清理 `{result.removed_jobs}` 个终态任务缓存，"
                f"释放 `{self._human_bytes(result.removed_bytes)}`。\n"
                f"因归档未完成而保留：`{result.blocked_by_archive}`。"
            ),
            self._cache_buttons(),
        )
    async def _status_text(self, owner_id: int) -> str:
        counts = await self._repository.count_by_state(owner_id=owner_id)
        runtime_health = await self._repository.get_runtime_health()
        queue_control = await self._repository.get_queue_control()
        disk = shutil.disk_usage(self._settings.download_dir)
        terminal = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
        active = sum(count for state, count in counts.items() if state not in terminal)
        connected = runtime_health.get("telegram", {}).get("status") == "connected"
        lines = [
            "🔧 **系统状态**",
            "",
            "🟢 运行正常" if connected else "🔴 和 Telegram 的连接断了，会自动重连",
            f"⏯ 处理：{'⏸ 已暂停' if queue_control.paused else '运行中'}",
            f"🚦 自动发布：{'开' if self._settings.publish_enabled else '关'}",
            f"💽 剩余空间：{self._human_bytes(disk.free)} / {self._human_bytes(disk.total)}",
        ]
        if self._cache_operator is not None:
            try:
                cache = await self._cache_operator.stats()
                lines.append(
                    f"🧹 临时文件：{self._human_bytes(cache.bytes_used)}"
                    + (f"（{cache.eligible_jobs} 个任务的可以清理）" if cache.eligible_jobs else "")
                )
            except Exception:
                pass
        lines.append(
            f"📦 任务：进行中 {active} · 完成 {counts.get(JobState.SUCCEEDED, 0)} · "
            f"失败 {counts.get(JobState.FAILED, 0)}"
        )
        problems = await self._recent_problem_lines()
        if problems:
            lines.extend(["", "**最近出过的问题**", *problems])
        return "\n".join(lines)

    async def _recent_problem_lines(self, limit: int = 5) -> list[str]:
        reader = getattr(self, "_problem_log", None)
        if reader is None:
            return []
        try:
            rows = await reader.recent_problems()
        except Exception:
            return []
        grouped: dict[str, tuple[int, str]] = {}
        for row in rows:
            label = problem_label(row)
            count, _last = grouped.get(label, (0, ""))
            grouped[label] = (count + 1, str(row.get("ts") or ""))
        latest = sorted(grouped.items(), key=lambda pair: pair[1][1], reverse=True)[:limit]
        return [
            f"• {label}" + (f" ×{count}" if count > 1 else "") + f" · 最近 {self._problem_time(ts)}"
            for label, (count, ts) in latest
        ]

    @staticmethod
    def _problem_time(ts: str) -> str:
        try:
            moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return "时间未知"
        return moment.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")

    async def _stats_text(self, owner_id: int) -> str:
        stats = await self._repository.get_stats_snapshot(owner_id=owner_id)
        events = await self._repository.get_recent_event_counts(owner_id=owner_id, hours=24)
        lines = [
            "**TGVIO 统计**",
            "",
            f"任务总数：`{stats.get('jobs_total', 0)}`",
            f"媒体项目：`{stats.get('media_items', 0)}`",
            f"已处理媒体大小：`{self._human_bytes(stats.get('media_bytes', 0))}`",
            f"完成 / 失败 / 取消：`{stats.get('succeeded', 0)}` / `{stats.get('failed', 0)}` / `{stats.get('cancelled', 0)}`",
            "",
            "**今天（UTC）**",
            f"创建：`{stats.get('today_jobs', 0)}`",
            f"完成：`{stats.get('today_succeeded', 0)}` · 失败：`{stats.get('today_failed', 0)}` · 取消：`{stats.get('today_cancelled', 0)}`",
        ]
        if events:
            lines.extend(["", "**最近 24h 事件**"])
            for event_type, count in list(events.items())[:8]:
                lines.append(f"• `{event_type}`：`{count}`")
        return "\n".join(lines)

    async def _health_text(self) -> str:
        runtime = await self._repository.get_runtime_health()
        db_ok = await self._repository.quick_check()
        disk = shutil.disk_usage(self._settings.download_dir)
        runtime_state = runtime.get("runtime", {})
        telegram_state = runtime.get("telegram", {})
        telegram_ok = telegram_state.get("status") == "connected"
        heartbeat_ok = runtime_state.get("status") == "alive"
        overall = db_ok and telegram_ok and heartbeat_ok
        lines = [
            "**TGVIO Health**",
            "",
            f"总体：`{'healthy' if overall else 'degraded'}`",
            f"SQLite：`{'ok' if db_ok else 'failed'}`",
            f"Runtime heartbeat：`{runtime_state.get('status', 'unknown')}`",
            f"Telegram：`{telegram_state.get('status', 'unknown')}`",
            f"磁盘可用：`{self._human_bytes(disk.free)}` / `{self._human_bytes(disk.total)}`",
        ]
        last_seen = telegram_state.get("updated_at")
        if last_seen:
            lines.append(f"最近 Telegram heartbeat：`{last_seen}` UTC")
        lines.extend(
            [
                "",
                "本页只读取本地 durable health，不会主动发送 Telegram 探测消息或访问 WebDAV。",
            ]
        )
        return "\n".join(lines)

    async def _cache_text(self) -> str:
        if self._cache_operator is None:
            return "**本地缓存**\n\n缓存维护服务未启用。"
        stats = await self._cache_operator.stats()
        return (
            "**本地缓存**\n\n"
            f"占用：`{self._human_bytes(stats.bytes_used)}`\n"
            f"Job 目录：`{stats.managed_dirs}`\n"
            f"自动保留：`{stats.retention_hours}` 小时\n"
            f"到期可清理：`{stats.eligible_jobs}`\n"
            f"被 Archive 阻塞：`{stats.blocked_by_archive}`\n\n"
            "自动清理只处理 SUCCEEDED/CANCELLED，不会删除 PLANNED/FAILED 的重试缓存。\n"
            "需要立即释放空间时，点下方“清理已完成缓存”。"
        )

    async def _cache_command(self, event, argument: str) -> None:
        if self._cache_operator is None:
            await event.respond("缓存维护服务未启用。")
            return
        if not argument:
            await event.respond(
                await self._cache_text(),
                buttons=self._home_buttons(),
                parse_mode="md",
            )
            return
        if argument.strip().lower() != "clean":
            await event.respond("用法：`/cache` 或 `/cache clean`", parse_mode="md")
            return
        result = await self._cache_operator.cleanup(force=True)
        await event.respond(
            (
                f"🧹 已清理 `{result.removed_jobs}` 个终态任务缓存，"
                f"释放 `{self._human_bytes(result.removed_bytes)}`。\n"
                f"Archive 阻塞：`{result.blocked_by_archive}`。"
            ),
            parse_mode="md",
        )

    async def _status_page_buttons(self):
        queue = await self._repository.get_queue_control()
        rows = [
            [
                Button.inline(
                    "▶️ 继续处理" if queue.paused else "⏸ 暂停处理",
                    b"ui:queue-resume" if queue.paused else b"ui:queue-pause",
                ),
                Button.inline("🧹 清理临时文件", b"ui:cache-clean"),
            ],
            [Button.inline("☁️ 云端备份", b"ui:archive"), Button.inline("📈 统计", b"ui:stats")],
            [Button.inline("🩺 技术诊断", b"ui:diag"), Button.inline("🔄 刷新", b"ui:status")],
            [Button.inline("⚙️ 设置", b"ui:settings"), Button.inline("🏠 首页", b"ui:home")],
        ]
        return rows
