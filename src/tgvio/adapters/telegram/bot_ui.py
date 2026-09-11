from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Callable

from telethon import Button, TelegramClient, events
from telethon.tl import functions, types

from tgvio.application.execution import PublishExecutionEngine
from tgvio.application.job_diagnostics import JobDiagnosticService, JobDiagnosticSnapshot
from tgvio.application.job_control import JobControlService, UnsafeRetryError
from tgvio.application.ports import ArchiveOperator, CacheOperator, JobRepository
from tgvio.config import Settings
from tgvio.domain.archive import ArchivePackageState
from tgvio.domain.job import Job, JobState
from tgvio.domain.publish import PublishPlan, PublishStepKind, PublishStepState, PublishTarget
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event


COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "打开 TGVIO 首页"),
    ("status", "查看运行与任务状态"),
    ("jobs", "查看最近任务"),
    ("job", "查看任务诊断详情"),
    ("plan", "查看最新发布计划"),
    ("stats", "查看任务统计"),
    ("health", "查看运行健康状态"),
    ("diag", "查看脱敏诊断信息"),
    ("retry", "安全重试失败任务"),
    ("cancel", "取消活动任务"),
    ("cache", "查看或清理本地缓存"),
    ("archive", "查看 WebDAV Archive 状态"),
    ("help", "查看使用说明"),
)


STATE_LABELS = {
    JobState.RECEIVED: "已接收",
    JobState.DOWNLOADING: "下载中",
    JobState.DOWNLOADED: "已下载",
    JobState.ANALYZING: "分析中",
    JobState.ANALYZED: "已分析",
    JobState.PLANNED: "已规划",
    JobState.PUBLISHING: "发布中",
    JobState.SUCCEEDED: "已完成",
    JobState.FAILED: "失败",
    JobState.CANCELLED: "已取消",
}


ARCHIVE_STATE_LABELS = {
    ArchivePackageState.PLANNED: "已规划",
    ArchivePackageState.STAGING: "准备中",
    ArchivePackageState.UPLOADING: "归档中",
    ArchivePackageState.VERIFYING: "校验中",
    ArchivePackageState.COMMITTED: "已完成",
    ArchivePackageState.FAILED: "失败",
    ArchivePackageState.CANCELLED: "已取消",
}


STEP_LABELS = {
    PublishStepKind.CHANNEL_COVER_ALBUM: "频道封面相册",
    PublishStepKind.CHANNEL_VIDEO_COVER: "频道视频帧封面",
    PublishStepKind.CHANNEL_MEDIA_GROUP: "频道媒体组",
    PublishStepKind.CHANNEL_DOCUMENT: "频道文件",
    PublishStepKind.DISCUSSION_PHOTO_ALBUM: "评论区图片组",
    PublishStepKind.DISCUSSION_VIDEO_ALBUM: "评论区视频组",
    PublishStepKind.DISCUSSION_MEDIA: "评论区媒体",
    PublishStepKind.DISCUSSION_DOCUMENT: "评论区文件",
}


class TelethonBotUI:
    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        repository: JobRepository,
        fixture_execution: PublishExecutionEngine | None = None,
        control: JobControlService | None = None,
        schedule_job: Callable | None = None,
        archive_operator: ArchiveOperator | None = None,
        cache_operator: CacheOperator | None = None,
        job_diagnostics: JobDiagnosticService | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._repository = repository
        self._fixture_execution = fixture_execution
        self._control = control
        self._schedule_job = schedule_job
        self._archive_operator = archive_operator
        self._cache_operator = cache_operator
        self._job_diagnostics = job_diagnostics or JobDiagnosticService(repository)
        self._log = logging.getLogger("tgvio.telegram.ui")
        self._tasks: set[asyncio.Task] = set()

    def register(self) -> None:
        self._client.add_event_handler(
            self._on_command,
            events.NewMessage(incoming=True, func=lambda event: (event.raw_text or "").lstrip().startswith("/")),
        )
        self._client.add_event_handler(self._on_callback, events.CallbackQuery(pattern=b"^ui:"))

    async def stop(self) -> None:
        if not self._tasks:
            return
        pending = tuple(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def configure_server_menu(self) -> None:
        default_scope = types.BotCommandScopeDefault()
        reset_scopes = (
            default_scope,
            types.BotCommandScopeUsers(),
            types.BotCommandScopeChats(),
            types.BotCommandScopeChatAdmins(),
        )
        for scope in reset_scopes:
            for lang_code in ("", "zh", "en"):
                try:
                    await self._client(
                        functions.bots.ResetBotCommandsRequest(scope=scope, lang_code=lang_code)
                    )
                except Exception as exc:
                    log_event(
                        self._log,
                        logging.WARNING,
                        "telegram.menu.reset_failed",
                        "Failed to reset bot commands",
                        scope=type(scope).__name__,
                        language=lang_code or "default",
                        exception_type=type(exc).__name__,
                        exc_info=True,
                    )
        commands = [types.BotCommand(command=name, description=description) for name, description in COMMANDS]
        for lang_code in ("", "zh", "en"):
            await self._client(
                functions.bots.SetBotCommandsRequest(
                    scope=default_scope,
                    lang_code=lang_code,
                    commands=commands,
                )
            )
        try:
            await self._client(
                functions.bots.SetBotMenuButtonRequest(
                    user_id=types.InputUserEmpty(),
                    button=types.BotMenuButtonCommands(),
                )
            )
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "telegram.menu.button_failed",
                "Failed to set bot menu button",
                exception_type=type(exc).__name__,
                exc_info=True,
            )

    async def _on_command(self, event) -> None:
        if not self._authorized(event.sender_id):
            return
        raw = (event.raw_text or "").strip()
        head, _, argument = raw.partition(" ")
        command = head[1:].split("@", 1)[0].lower()
        if command == "start":
            await event.respond(self._home_text(), buttons=self._home_buttons(), parse_mode="md")
        elif command == "help":
            await event.respond(self._help_text(), buttons=self._home_buttons(), parse_mode="md")
        elif command == "status":
            await event.respond(await self._status_text(int(event.sender_id)), buttons=self._home_buttons(), parse_mode="md")
        elif command == "jobs":
            await event.respond(await self._jobs_text(int(event.sender_id)), buttons=self._home_buttons(), parse_mode="md")
        elif command == "job":
            await event.respond(
                await self._job_text(int(event.sender_id), argument.strip() or None),
                buttons=self._home_buttons(),
                parse_mode="md",
            )
        elif command == "plan":
            await event.respond(
                await self._plan_text(int(event.sender_id), argument.strip() or None),
                buttons=self._home_buttons(),
                parse_mode="md",
            )
        elif command == "stats":
            await event.respond(
                await self._stats_text(int(event.sender_id)),
                buttons=self._home_buttons(),
                parse_mode="md",
            )
        elif command == "health":
            await event.respond(
                await self._health_text(),
                buttons=self._home_buttons(),
                parse_mode="md",
            )
        elif command == "diag":
            diag_argument = argument.strip()
            if diag_argument.lower().startswith("job "):
                text = await self._job_text(
                    int(event.sender_id),
                    diag_argument[4:].strip() or None,
                    deep=True,
                )
            else:
                text = await self._diag_text()
            await event.respond(
                text,
                buttons=self._home_buttons(),
                parse_mode="md",
            )
        elif command == "retry":
            await self._retry_job(event, int(event.sender_id), argument.strip() or None)
        elif command == "cancel":
            await self._cancel_job(event, int(event.sender_id), argument.strip() or None)
        elif command == "archive":
            await self._archive_command(event, int(event.sender_id), argument.strip())
        elif command == "cache":
            await self._cache_command(event, argument.strip())
        elif command == "publish":
            await self._request_fixture_publish(
                event,
                int(event.sender_id),
                argument.strip() or None,
            )

    async def _on_callback(self, event) -> None:
        if not self._authorized(event.sender_id):
            await event.answer("无权限", alert=True)
            return
        action = bytes(event.data or b"").decode("utf-8", "replace")
        owner_id = int(event.sender_id)
        if action.startswith("ui:fixture-confirm:"):
            job_id = action.split(":", 2)[2]
            await self._confirm_fixture_publish(event, owner_id, job_id)
            return
        if action == "ui:fixture-cancel":
            await event.edit("已取消受控发布。", buttons=self._home_buttons())
            await event.answer()
            return
        if action == "ui:home":
            text = self._home_text()
        elif action == "ui:status":
            text = await self._status_text(owner_id)
        elif action == "ui:jobs":
            text = await self._jobs_text(owner_id)
        elif action == "ui:job":
            text = await self._job_text(owner_id, None)
        elif action == "ui:plan":
            text = await self._plan_text(owner_id, None)
        elif action == "ui:stats":
            text = await self._stats_text(owner_id)
        elif action == "ui:health":
            text = await self._health_text()
        elif action == "ui:diag":
            text = await self._diag_text()
        elif action == "ui:archive":
            text = await self._archive_text(owner_id)
        elif action == "ui:cache":
            text = await self._cache_text()
        elif action == "ui:help":
            text = self._help_text()
        else:
            await event.answer("未知操作")
            return
        await event.edit(text, buttons=self._home_buttons(), parse_mode="md")
        await event.answer()

    def _home_text(self) -> str:
        publish_status = "开启" if getattr(self._settings, "publish_enabled", False) else "关闭"
        return (
            "**TGVIO** · Telegram Video I/O\n\n"
            "新一代媒体任务编排器。\n"
            "当前流水线：`接收 → 下载 → 分析 → 发布规划`\n\n"
            "📥 直接发送图片、视频、文件或媒体组即可创建任务。\n"
            f"🔗 URL 下载：`{'开启' if self._settings.url_enabled else '关闭'}`。\n"
            f"☁️ WebDAV Archive：`{'开启' if self._settings.archive_enabled else '关闭'}`。\n"
            "🧠 每个任务都会先生成可审计的 PublishPlan。\n"
            f"🚦 发布执行：`{publish_status}`。\n"
            "🛡️ 发布关闭时任务会安全停在 PLANNED，不会产生频道副作用。"
        )

    def _help_text(self) -> str:
        text = (
            "**TGVIO 使用说明**\n\n"
            "`/start` — 首页\n"
            "`/status` — 运行状态与任务统计\n"
            "`/jobs` — 最近任务\n"
            "`/job [任务ID]` — 查看任务全链路状态与诊断提示\n"
            "`/plan [任务ID]` — 查看发布计划；ID 可只填前缀\n"
            "`/stats` — 今日/累计任务统计\n"
            "`/health` — SQLite、Telegram、磁盘健康状态\n"
            "`/diag` — 脱敏运行诊断；`/diag job 任务ID` 查看深度任务日志\n"
            "`/retry 任务ID` — 安全重试失败任务\n"
            "`/cancel 任务ID` — 请求取消活动任务\n"
            "`/cache` — 查看本地缓存；`/cache clean` 清理已完成/取消任务缓存\n"
            "`/archive` — 查看 WebDAV Archive 状态；支持 `retry` / `probe`\n"
            "`/help` — 本说明\n\n"
            "支持输入：图片、视频、普通文件、Telegram 媒体组。\n"
            "一个任务会依次持久化下载事实、媒体分析结果与发布步骤。"
        )
        if self._settings.url_enabled:
            text += "\n🔗 也可直接发送一个 HTTP(S) 媒体/站点链接，由 yt-dlp 下载后进入同一流水线。"
        if getattr(self._settings, "live_fixture_enabled", False):
            text += (
                "\n\n🧪 受控发布已启用：`/publish 任务ID`。"
                "该命令需要二次确认，且只允许小型安全 fixture。"
            )
        return text

    async def _status_text(self, owner_id: int) -> str:
        counts = await self._repository.count_by_state(owner_id=owner_id)
        runtime_health = await self._repository.get_runtime_health()
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

    async def _diag_text(self) -> str:
        runtime = await self._repository.get_runtime_health()
        cache = await self._cache_operator.stats() if self._cache_operator is not None else None
        summary = self._settings.safe_summary()
        lines = [
            "**TGVIO Diagnostics**",
            "",
            f"Commit：`{os.getenv('APP_COMMIT', 'unknown')}`",
            f"Environment：`{summary.get('environment', 'unknown')}`",
            f"Telegram：`{runtime.get('telegram', {}).get('status', 'unknown')}`",
            f"Publish：`{'on' if summary.get('publish_enabled') else 'off'}`",
            f"URL：`{'on' if summary.get('url_enabled') else 'off'}` / `{summary.get('url_private_network_policy', 'unknown')}`",
            f"Archive：`{'on' if summary.get('archive_enabled') else 'off'}`",
            f"Logs：`JSONL/{summary.get('log_level', 'INFO')}` · file `{'on' if summary.get('log_file_enabled') else 'off'}`",
            f"Workers：`{summary.get('worker_concurrency', 0)}`",
            f"Disk reserve：`{summary.get('disk_reserve_mb', 0)} MiB`",
        ]
        if cache is not None:
            lines.append(
                f"Managed cache：`{self._human_bytes(cache.bytes_used)}` / `{cache.managed_dirs}` dirs"
            )
        lines.extend(
            [
                "",
                "诊断输出不包含 Bot Token、API Hash、用户 ID、caption、完整 URL、WebDAV 凭据或本地媒体路径。",
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
            "需要立即释放已完成缓存可使用 `/cache clean`。"
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

    async def _archive_text(self, owner_id: int) -> str:
        enabled = bool(getattr(self._settings, "archive_enabled", False))
        lines = [
            "**WebDAV Archive V2**",
            "",
            f"状态：`{'开启' if enabled else '关闭'}`",
            f"远端根目录：`{getattr(self._settings, 'archive_remote_root', 'TGVIO')}`",
        ]
        counts = await self._repository.count_archive_packages_by_state(owner_id=owner_id)
        if counts:
            lines.extend(["", "**Packages**"])
            for state in ArchivePackageState:
                if counts.get(state, 0):
                    lines.append(f"• {ARCHIVE_STATE_LABELS[state]}：`{counts[state]}`")
        else:
            lines.extend(["", "还没有 ArchivePackage。"])

        recent = await self._repository.list_recent_archive_packages(owner_id=owner_id, limit=5)
        if recent:
            lines.extend(["", "**最近归档**"])
            for package in recent:
                stored = sum(1 for obj in package.objects if obj.state.value == "stored")
                total_bytes = sum(obj.size_bytes for obj in package.objects)
                lines.append(
                    f"• `{package.job_id[:10]}` · {ARCHIVE_STATE_LABELS[package.state]} · "
                    f"`{stored}/{len(package.objects)}` 文件 · {self._human_bytes(total_bytes)}"
                )
        lines.extend(
            [
                "",
                "一个 Job 永远只有一个 ArchivePackage；不会再产生 backup attempt 链。",
                "只有 `_COMPLETE.json` 存在且校验通过才视为归档完成。",
            ]
        )
        if enabled:
            lines.append("失败包可用 `/archive retry 任务ID` 显式恢复；`/archive probe` 只读检测 WebDAV 能力。")
        else:
            lines.append("Archive 当前关闭，不会发生任何 WebDAV 网络写入。")
        return "\n".join(lines)

    async def _archive_command(self, event, owner_id: int, argument: str) -> None:
        parts = argument.split()
        if not parts:
            await event.respond(
                await self._archive_text(owner_id),
                buttons=self._home_buttons(),
                parse_mode="md",
            )
            return
        action = parts[0].lower()
        if action == "probe" and len(parts) == 1:
            if not getattr(self._settings, "archive_enabled", False) or self._archive_operator is None:
                await event.respond("🛡️ WebDAV Archive 当前未启用。")
                return
            try:
                capabilities = await self._archive_operator.probe()
            except Exception as exc:
                await event.respond(f"❌ WebDAV capability probe 失败：`{type(exc).__name__}`", parse_mode="md")
                return
            await event.respond(
                (
                    "**WebDAV Capability Probe**\n\n"
                    f"PROPFIND：`{capabilities.supports_propfind}`\n"
                    f"MKCOL：`{capabilities.supports_mkcol}`\n"
                    f"PUT：`{capabilities.supports_put}`\n"
                    f"GET：`{capabilities.supports_get}`\n"
                    f"MOVE：`{capabilities.supports_move}`\n"
                    f"ETag：`{capabilities.supports_etag}`\n"
                    f"Commit mode：`{capabilities.commit_mode}`"
                ),
                parse_mode="md",
            )
            return
        if action != "retry" or len(parts) != 2:
            await event.respond(
                "用法：`/archive`、`/archive probe` 或 `/archive retry 任务ID`",
                parse_mode="md",
            )
            return
        if not getattr(self._settings, "archive_enabled", False) or self._archive_operator is None:
            await event.respond("🛡️ WebDAV Archive 当前未启用。")
            return
        job = await self._resolve_job(owner_id, parts[1])
        if job is None:
            await event.respond("没有找到唯一对应的任务。")
            return
        package = await self._repository.get_archive_package_for_job(job.id)
        if package is None:
            await event.respond("这个任务还没有 ArchivePackage。")
            return
        try:
            reset = await self._archive_operator.retry_package(package.id)
        except Exception as exc:
            await event.respond(f"🛡️ 无法恢复 ArchivePackage：`{type(exc).__name__}`", parse_mode="md")
            return
        await event.respond(
            f"🔁 ArchivePackage `{reset.id[-10:]}` 已恢复到 durable staging 队列。",
            parse_mode="md",
        )

    async def _jobs_text(self, owner_id: int) -> str:
        jobs = await self._repository.list_recent(owner_id=owner_id, limit=8)
        if not jobs:
            return "**最近任务**\n\n还没有任务。直接发送媒体即可开始。"
        lines = ["**最近任务**", ""]
        for job in jobs:
            size = sum(item.size_bytes for item in job.items)
            line = (
                f"`{job.id[:10]}` · {STATE_LABELS[job.state]} · "
                f"{len(job.items)} 项 · {self._human_bytes(size)}"
            )
            if job.state == JobState.FAILED:
                line += f" · `{job.error_code or 'unknown_error'}`"
            lines.append(line)
            progress = await self._repository.get_job_progress(job.id)
            if progress is not None and job.state not in {
                JobState.SUCCEEDED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                lines.append(f"  {self._progress_text(progress)}")
            hint = self._job_action_hint(job)
            if hint:
                lines.append(f"  {hint}")
        lines.extend(["", "使用 `/job 任务ID` 查看全链路诊断；`/plan 任务ID` 查看具体发布计划。"])
        return "\n".join(lines)

    async def _job_text(
        self,
        owner_id: int,
        prefix: str | None,
        *,
        deep: bool = False,
    ) -> str:
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            return "**任务诊断**\n\n没有找到唯一对应的任务。"
        snapshot = await self._job_diagnostics.inspect(
            job,
            log_limit=60 if deep else 16,
        )
        return self._render_job_diagnostic(snapshot, deep=deep)

    def _render_job_diagnostic(
        self,
        snapshot: JobDiagnosticSnapshot,
        *,
        deep: bool,
    ) -> str:
        job = snapshot.job
        size = sum(item.size_bytes for item in job.items)
        event_types = {event.event_type for event in snapshot.events}
        lines = [
            f"**任务诊断 `{job.id[:10]}`**",
            "",
            f"状态：**{STATE_LABELS[job.state]}**",
            f"媒体：`{len(job.items)}` 项 · `{self._human_bytes(size)}`",
        ]
        if job.created_at:
            lines.append(f"创建：`{job.created_at}` UTC")
        if job.updated_at:
            lines.append(f"更新：`{job.updated_at}` UTC")
        if job.error_code:
            lines.append(f"错误码：`{job.error_code}`")
        if snapshot.progress is not None and not job.terminal:
            lines.append(self._progress_text(snapshot.progress))

        lines.extend(
            [
                "",
                "**Pipeline**",
                f"{self._phase_status(event_types, 'job_created', None)} Intake",
                f"{self._phase_status(event_types, 'download_completed', 'download_failed', 'download_started')} Download",
                f"{self._phase_status(event_types, 'analysis_completed', 'analysis_failed', 'analysis_started')} Analysis",
                f"{'✅' if snapshot.plan is not None else '▫️'} PublishPlan",
                f"{self._publish_status(snapshot)} Publish",
                f"{self._archive_status(snapshot)} Archive",
            ]
        )

        if snapshot.plan is not None:
            succeeded = sum(
                1 for step in snapshot.plan.steps if step.state == PublishStepState.SUCCEEDED
            )
            failed = [step for step in snapshot.plan.steps if step.state == PublishStepState.FAILED]
            external_effects = [
                effect
                for effect in snapshot.effects
                if effect.effect_type != "publish_step_receipts_committed"
            ]
            lines.extend(
                [
                    "",
                    "**Publish**",
                    f"Plan：`{snapshot.plan.id[:10]}` · steps `{succeeded}/{len(snapshot.plan.steps)}` · effects `{len(external_effects)}`",
                ]
            )
            for step in failed[:3]:
                confirmed = sum(
                    1
                    for effect in external_effects
                    if effect.step_index == step.index
                )
                lines.append(
                    f"⚠️ step `{step.index}` · `{step.kind.value}` · `{step.error_code or 'failed'}` · confirmed `{confirmed}`"
                )

        if snapshot.archive is not None:
            archive = snapshot.archive
            stored = sum(1 for obj in archive.objects if obj.state.value == "stored")
            failed_objects = [obj for obj in archive.objects if obj.state.value == "failed"]
            lines.extend(
                [
                    "",
                    "**Archive**",
                    f"Package：`{archive.id[-10:]}` · {ARCHIVE_STATE_LABELS[archive.state]}",
                    f"Objects：`{stored}/{len(archive.objects)}` · `{self._human_bytes(sum(obj.size_bytes for obj in archive.objects))}` · events `{snapshot.archive_event_count}`",
                ]
            )
            if archive.error_code:
                lines.append(f"错误码：`{archive.error_code}`")
            if failed_objects:
                indexes = ", ".join(str(obj.object_index) for obj in failed_objects[:8])
                lines.append(f"失败对象：`{indexes}`")

        if snapshot.hints:
            icons = {
                "critical": "🔴",
                "error": "🔴",
                "warning": "🟡",
                "info": "🟢",
            }
            lines.extend(["", "**Diagnosis**"])
            for hint in snapshot.hints:
                lines.append(f"{icons.get(hint.severity, '•')} `{hint.code}` · {hint.summary}")
                if hint.action:
                    lines.append(f"  ↳ {hint.action}")

        recent_events = snapshot.events[-(8 if deep else 4) :]
        if recent_events:
            lines.extend(["", "**Durable events**"])
            for event in recent_events:
                timestamp = event.created_at or "?"
                lines.append(f"• `{timestamp}` · `{event.event_type}`")

        if deep:
            lines.extend(["", "**Structured logs**"])
            recent_logs = snapshot.recent_logs[-10:]
            if not recent_logs:
                lines.append("没有找到该 Job 的持久结构化日志。")
            for row in recent_logs:
                extras: list[str] = []
                if row.get("step_index") is not None:
                    extras.append(f"step={row['step_index']}")
                if row.get("object_index") is not None:
                    extras.append(f"object={row['object_index']}")
                if row.get("error_code"):
                    extras.append(str(row["error_code"]))
                suffix = f" · `{' · '.join(extras)}`" if extras else ""
                lines.append(
                    f"• `{row.get('ts', '?')}` · `{row.get('level', '?')}` · `{row.get('event', 'log.message')}`{suffix}"
                )
            lines.extend(
                [
                    "",
                    "这里只展示已经过日志脱敏层处理的事件元数据，不显示 caption、URL、凭据或本地媒体路径。",
                ]
            )
        else:
            lines.extend(["", f"深度日志：`/diag job {job.id[:10]}`"])

        text = "\n".join(lines)
        return text if len(text) <= 3900 else text[:3850] + "\n\n…诊断输出已截断。"

    @staticmethod
    def _phase_status(
        event_types: set[str],
        success_event: str,
        failure_event: str | None,
        started_event: str | None = None,
    ) -> str:
        if success_event in event_types:
            return "✅"
        if failure_event and failure_event in event_types:
            return "❌"
        if started_event and started_event in event_types:
            return "⏳"
        return "▫️"

    @staticmethod
    def _publish_status(snapshot: JobDiagnosticSnapshot) -> str:
        if snapshot.job.state == JobState.SUCCEEDED or "publish_completed" in {
            event.event_type for event in snapshot.events
        }:
            return "✅"
        if snapshot.job.error_code in {
            "publish_failed",
            "publish_partial",
            "publish_uncertain",
        }:
            return "❌"
        if snapshot.job.state == JobState.PUBLISHING:
            return "⏳"
        if snapshot.plan is not None:
            return "▫️"
        return "▫️"

    @staticmethod
    def _archive_status(snapshot: JobDiagnosticSnapshot) -> str:
        if snapshot.archive is None:
            return "▫️"
        if snapshot.archive.state == ArchivePackageState.COMMITTED:
            return "✅"
        if snapshot.archive.state == ArchivePackageState.FAILED:
            return "❌"
        if snapshot.archive.state == ArchivePackageState.CANCELLED:
            return "⛔"
        return "⏳"

    def _progress_text(self, progress: JobProgress) -> str:
        phase_labels = {
            "queued": "排队",
            "downloading": "下载",
            "downloaded": "已下载",
            "analyzing": "分析",
            "analyzed": "已分析",
            "planned": "已规划",
            "publishing": "发布",
            "succeeded": "完成",
            "failed": "失败",
            "cancelled": "取消",
        }
        label = phase_labels.get(progress.phase, progress.phase)
        details: list[str] = []
        if progress.item_index is not None and progress.item_total > 0:
            details.append(f"媒体 {progress.item_index + 1}/{progress.item_total}")
        if progress.total > 0:
            if progress.phase == "downloading":
                pct = min(100, int(progress.current * 100 / progress.total))
                details.append(
                    f"{pct}% · {self._human_bytes(progress.current)}/{self._human_bytes(progress.total)}"
                )
            else:
                details.append(f"{progress.current}/{progress.total}")
        return f"⏱ {label}" + (f" · {' · '.join(details)}" if details else "")

    async def _plan_text(self, owner_id: int, prefix: str | None) -> str:
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            return "**发布计划**\n\n没有找到对应任务。"
        plan = await self._repository.get_publish_plan(job.id)
        if plan is None:
            return (
                f"**发布计划 `{job.id[:10]}`**\n\n"
                f"当前状态：{STATE_LABELS[job.state]}\n"
                "该任务还没有生成 PublishPlan。"
            )
        return self._render_plan(job, plan)

    async def _resolve_job(self, owner_id: int, prefix: str | None) -> Job | None:
        jobs = await self._repository.list_recent(owner_id=owner_id, limit=50)
        if not jobs:
            return None
        if not prefix:
            return next((job for job in jobs if job.state in {JobState.PLANNED, JobState.PUBLISHING, JobState.SUCCEEDED}), jobs[0])
        normalized = prefix.strip().lower()
        matches = [job for job in jobs if job.id.lower().startswith(normalized)]
        return matches[0] if len(matches) == 1 else None

    async def _cancel_job(self, event, owner_id: int, prefix: str | None) -> None:
        if self._control is None:
            await event.respond("任务控制服务未启用。")
            return
        if not prefix:
            await event.respond("请使用 `/cancel 任务ID`，取消操作必须显式指定任务。", parse_mode="md")
            return
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            await event.respond("没有找到唯一对应的任务。")
            return
        try:
            current = await self._control.request_cancel(
                job,
                reason=f"requested by owner {owner_id}",
            )
        except ValueError as exc:
            await event.respond(f"🛡️ 无法取消：{exc}")
            return
        if current.state == JobState.CANCELLED:
            await event.respond(f"⛔ 任务 `{job.id[:10]}` 已取消。", parse_mode="md")
        else:
            await event.respond(
                f"⏳ 任务 `{job.id[:10]}` 已记录取消请求，将在当前安全边界停止。",
                parse_mode="md",
            )

    async def _retry_job(self, event, owner_id: int, prefix: str | None) -> None:
        if self._control is None:
            await event.respond("任务控制服务未启用。")
            return
        if not prefix:
            await event.respond("请使用 `/retry 任务ID`，重试操作必须显式指定任务。", parse_mode="md")
            return
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            await event.respond("没有找到唯一对应的任务。")
            return
        try:
            decision = await self._control.retry_failed(job)
        except (ValueError, UnsafeRetryError) as exc:
            await event.respond(f"🛡️ 无法安全重试：{exc}")
            return

        if decision.target_state == JobState.RECEIVED and self._schedule_job is not None:
            self._schedule_job(decision.job, chat_id=event.chat_id)
            suffix = "已重新进入下载/分析流水线。"
        elif decision.target_state == JobState.PLANNED and self._settings.publish_enabled and self._schedule_job is not None:
            self._schedule_job(decision.job, chat_id=event.chat_id)
            suffix = "已从失败的发布 step 继续执行。"
        else:
            suffix = "已恢复到 PLANNED；当前发布执行关闭，不会自动产生频道消息。"
        await event.respond(
            f"🔁 任务 `{job.id[:10]}` 已创建第 `{decision.retry_count}` 次安全重试。\n{suffix}",
            parse_mode="md",
        )

    async def _request_fixture_publish(
        self,
        event,
        owner_id: int,
        prefix: str | None,
    ) -> None:
        if self._fixture_execution is None or not getattr(
            self._settings, "live_fixture_enabled", False
        ):
            await event.respond("🛡️ 受控真实发布未启用。")
            return
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            await event.respond("没有找到唯一对应的任务。")
            return
        plan = await self._repository.get_publish_plan(job.id)
        if plan is None:
            await event.respond("该任务还没有 PublishPlan。")
            return
        error = self._fixture_validation_error(job, plan)
        if error:
            await event.respond(f"🛡️ 不能用于受控发布：{error}")
            return
        await event.respond(
            (
                f"**受控真实发布确认** `{job.id[:10]}`\n\n"
                f"媒体：`{len(job.items)}` 项\n"
                f"总大小：`{self._human_bytes(sum(item.size_bytes for item in job.items))}`\n"
                "确认后会对真实目标频道产生 Telegram 消息。"
            ),
            buttons=[
                [
                    Button.inline(
                        "⚠️ 确认真实发布",
                        f"ui:fixture-confirm:{job.id}".encode(),
                    ),
                    Button.inline("取消", b"ui:fixture-cancel"),
                ]
            ],
            parse_mode="md",
        )

    async def _confirm_fixture_publish(self, event, owner_id: int, job_id: str) -> None:
        if self._fixture_execution is None or not getattr(
            self._settings, "live_fixture_enabled", False
        ):
            await event.answer("受控发布未启用", alert=True)
            return
        job = await self._repository.get(job_id)
        if job is None or job.owner_id != owner_id:
            await event.answer("任务不存在或无权限", alert=True)
            return
        plan = await self._repository.get_publish_plan(job.id)
        if plan is None:
            await event.answer("发布计划不存在", alert=True)
            return
        error = self._fixture_validation_error(job, plan)
        if error:
            await event.answer(error, alert=True)
            return
        await event.answer("受控发布已开始")
        await event.edit(
            f"🧪 任务 `{job.id[:10]}` 正在执行受控真实发布……",
            buttons=self._home_buttons(),
            parse_mode="md",
        )
        task = asyncio.create_task(
            self._run_fixture_publish(event.chat_id, job, plan),
            name=f"tgvio-live-fixture-{job.id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_fixture_publish(self, chat_id: int, job: Job, plan: PublishPlan) -> None:
        assert self._fixture_execution is not None
        try:
            completed = await self._fixture_execution.execute(job, plan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                self._log,
                logging.ERROR,
                "publish.fixture.failed",
                "Controlled live fixture failed",
                job_id=job.id,
                plan_id=plan.id,
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            await self._client.send_message(
                chat_id,
                f"❌ 受控发布 `{job.id[:10]}` 失败：`{type(exc).__name__}`",
                parse_mode="md",
            )
            return
        await self._client.send_message(
            chat_id,
            f"✅ 受控发布 `{completed.id[:10]}` 完成。",
            parse_mode="md",
        )

    def _fixture_validation_error(self, job: Job, plan: PublishPlan) -> str | None:
        if job.state != JobState.PLANNED:
            return f"任务状态必须是 PLANNED，当前为 {job.state.value}"
        if len(job.items) > 10:
            return "fixture 最多允许 10 个媒体"
        total_bytes = sum(item.size_bytes for item in job.items)
        if total_bytes > getattr(self._settings, "live_fixture_max_bytes", 100 * 1024 * 1024):
            return "fixture 总大小超过安全上限"
        unsafe = {
            str(strategy)
            for step in plan.steps
            for strategy in step.params.get("strategies", {}).values()
            if str(strategy) in {"split_playable", "binary_volume"}
        }
        if unsafe:
            return "fixture 不允许大文件分段/分卷策略"
        return None

    def _render_plan(self, job: Job, plan: PublishPlan) -> str:
        summary = plan.summary
        lines = [
            f"**PublishPlan `{job.id[:10]}` · v{plan.version}**",
            "",
            f"媒体：`{summary.get('media_total', len(job.items))}` · "
            f"图片 `{summary.get('photos', 0)}` · 视频 `{summary.get('videos', 0)}` · 文件 `{summary.get('documents', 0)}`",
            f"频道步骤：`{summary.get('channel_steps', 0)}` · 评论区步骤：`{summary.get('discussion_steps', 0)}`",
            "",
        ]
        for step in plan.steps:
            target = "频道" if step.target == PublishTarget.CHANNEL else "评论区"
            indexes = ",".join(str(index + 1) for index in step.item_indexes)
            strategies = sorted(set(str(value) for value in step.params.get("strategies", {}).values()))
            strategy_text = "/".join(strategies) if strategies else "native"
            state_mark = {
                PublishStepState.PENDING: "▫️",
                PublishStepState.RUNNING: "🔄",
                PublishStepState.SUCCEEDED: "✅",
                PublishStepState.FAILED: "❌",
                PublishStepState.SKIPPED: "⏭️",
            }[step.state]
            lines.append(
                f"{state_mark} `{step.index + 1}` {target} · {STEP_LABELS[step.kind]} · "
                f"媒体 `{indexes}` · `{strategy_text}`"
            )
        action_hint = self._job_action_hint(job)
        if action_hint:
            lines.extend(["", action_hint])
        if self._settings.publish_enabled:
            lines.extend(["", "发布执行已开启；步骤状态会随 durable effects 更新。"])
        else:
            lines.extend(["", "发布执行当前关闭；计划只读且不会产生 Telegram 副作用。"])
        return "\n".join(lines)

    @staticmethod
    def _job_action_hint(job: Job) -> str:
        if job.state == JobState.FAILED:
            if job.error_code in {"publish_partial", "publish_uncertain"}:
                return "🛡️ 已检测到可能存在 Telegram 副作用；禁止盲重试，需要人工核对。"
            return f"🔁 可使用 `/retry {job.id[:10]}` 请求安全重试；系统会再次校验副作用。"
        if job.state in {
            JobState.RECEIVED,
            JobState.DOWNLOADING,
            JobState.DOWNLOADED,
            JobState.ANALYZING,
            JobState.ANALYZED,
            JobState.PLANNED,
            JobState.PUBLISHING,
        }:
            return f"⛔ 使用 `/cancel {job.id[:10]}` 请求在安全边界取消。"
        return ""

    @staticmethod
    def _human_bytes(value: int) -> str:
        amount = float(max(0, value))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
            amount /= 1024
        return f"{amount:.1f}TB"

    def _home_buttons(self):
        return [
            [Button.inline("📊 状态", b"ui:status"), Button.inline("📋 最近任务", b"ui:jobs")],
            [Button.inline("🔎 任务诊断", b"ui:job"), Button.inline("🧠 最新计划", b"ui:plan")],
            [Button.inline("📈 统计", b"ui:stats"), Button.inline("❤️ 健康", b"ui:health")],
            [Button.inline("🧹 缓存", b"ui:cache"), Button.inline("☁️ Archive", b"ui:archive")],
            [Button.inline("🩺 诊断", b"ui:diag"), Button.inline("ℹ️ 帮助", b"ui:help")],
            [Button.inline("🏠 首页", b"ui:home")],
        ]

    def _authorized(self, sender_id: int | None) -> bool:
        return sender_id is not None and int(sender_id) in self._settings.allowed_users

