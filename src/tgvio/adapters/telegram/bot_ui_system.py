from __future__ import annotations

import logging
import shutil

from telethon import Button

from tgvio.application.operation_tokens import OperationTokenInvalidError
from tgvio.domain.job import JobState
from tgvio.observability import log_event
from tgvio.adapters.telegram.bot_ui_support import STATE_LABELS


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
        active_states = {
            JobState.RECEIVED,
            JobState.DOWNLOADING,
            JobState.DOWNLOADED,
            JobState.ANALYZING,
            JobState.ANALYZED,
            JobState.PLANNED,
            JobState.PUBLISHING,
        }
        active = sum(counts.get(state, 0) for state in active_states)
        total = sum(counts.values())
        telegram_status = str(
            runtime_health.get("telegram", {}).get("status", "unknown")
        )
        telegram_label = {
            "connected": "🟢 已连接",
            "disconnected": "🔴 已断开",
        }.get(telegram_status, "🟡 未知")
        lines = [
            "**TGVIO 状态**",
            "",
            f"Telegram：{telegram_label}",
            f"⚙️ 环境：`{self._settings.environment}`",
            f"🚦 发布执行：`{'开启' if self._settings.publish_enabled else '关闭'}`",
            f"⏯ 队列：`{'已暂停' if queue_control.paused else '运行中'}`",
            f"🧪 受控发布：`{'开启' if getattr(self._settings, 'live_fixture_enabled', False) else '关闭'}`",
            f"🔗 URL 下载：`{'开启' if self._settings.url_enabled else '关闭'}` · `{self._settings.url_private_network_policy}`",
            f"🧵 Worker：`{self._settings.worker_concurrency}`",
            f"📦 任务：`{total}`，活跃：`{active}`",
            f"💽 磁盘可用：`{self._human_bytes(disk.free)}` / `{self._human_bytes(disk.total)}`",
        ]
        if counts:
            lines.extend(["", "**任务状态**"])
            for state in JobState:
                count = counts.get(state, 0)
                if count:
                    lines.append(f"• {STATE_LABELS[state]}：`{count}`")
        return "\n".join(lines)

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
                    "▶️ 恢复队列" if queue.paused else "⏸ 暂停新任务",
                    b"ui:queue-resume" if queue.paused else b"ui:queue-pause",
                )
            ]
        ]
        rows.extend(self._nav_buttons())
        return rows
