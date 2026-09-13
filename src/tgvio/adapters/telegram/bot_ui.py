from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
import shutil
from typing import Callable
from zoneinfo import ZoneInfo

from telethon import Button, TelegramClient, events
from telethon.tl import functions, types

from tgvio.adapters.telegram.user_messages import (
    describe_archive_failure,
    describe_job_failure,
)
from tgvio.application.auto_recovery import (
    archive_failure_waits_for_recovery,
    archive_recovery_state,
    job_failure_waits_for_recovery,
    job_recovery_state,
)
from tgvio.application.execution import PublishExecutionEngine
from tgvio.application.job_diagnostics import JobDiagnosticService, JobDiagnosticSnapshot
from tgvio.application.job_control import JobControlService, UnsafeRetryError
from tgvio.application.operation_tokens import (
    OperationTokenInvalidError,
    OperationTokenService,
)
from tgvio.application.ports import ArchiveOperator, CacheOperator, JobRepository
from tgvio.application.undo import (
    UndoOperationInvalidError,
    UndoService,
    UndoUnavailableError,
)
from tgvio.config import Settings
from tgvio.domain.archive import ArchivePackageState
from tgvio.domain.job import Job, JobState, MediaKind
from tgvio.domain.job_query import (
    FailurePage,
    FailureSummary,
    JobListEntry,
    JobListFilter,
    JobPage,
)
from tgvio.domain.publish import PublishPlan, PublishStepKind, PublishStepState, PublishTarget
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event


COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "打开 TGVIO 首页"),
    ("begin", "开始收集一个合集"),
    ("end", "结束合集并创建任务"),
    ("mode", "设置雪花显示偏好"),
    ("pause", "暂停队列或指定任务"),
    ("resume", "恢复队列或指定任务"),
    ("jobs", "查看最近任务"),
    ("status", "查看运行与任务状态"),
    ("help", "查看使用说明"),
)


NAV_HOME = "🏠 首页"
NAV_JOBS = "📋 我的任务"
NAV_STATUS = "📊 状态"
NAV_ARCHIVE = "☁️ 归档"
NAV_CACHE = "🧹 缓存"
NAV_MORE = "ℹ️ 更多"
COLLECTION_BEGIN_BUTTON = "📥 开始合集"
COLLECTION_END_BUTTON = "🛑 结束并发布"
NAV_BUTTONS = frozenset(
    {NAV_HOME, NAV_JOBS, NAV_STATUS, NAV_ARCHIVE, NAV_CACHE, NAV_MORE}
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


JOB_FILTER_LABELS = {
    JobListFilter.ALL: "全部",
    JobListFilter.ACTIVE: "进行中",
    JobListFilter.HELD: "暂停",
    JobListFilter.FAILED: "失败",
    JobListFilter.COMPLETED: "完成",
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
        undo_service: UndoService | None = None,
        operation_tokens: OperationTokenService | None = None,
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
        self._undo_service = undo_service
        self._operation_tokens = operation_tokens
        self._log = logging.getLogger("tgvio.telegram.ui")
        self._tasks: set[asyncio.Task] = set()

    def register(self) -> None:
        self._client.add_event_handler(
            self._on_command,
            events.NewMessage(incoming=True, func=lambda event: (event.raw_text or "").lstrip().startswith("/")),
        )
        self._client.add_event_handler(
            self._on_nav_button,
            events.NewMessage(
                incoming=True,
                func=lambda event: (event.raw_text or "").strip() in NAV_BUTTONS,
            ),
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
            await event.respond(
                self._home_text(),
                buttons=self._reply_keyboard(),
                parse_mode="md",
            )
        elif command == "help":
            await event.respond(
                self._help_text(),
                buttons=self._more_buttons(),
                parse_mode="md",
            )
        elif command == "status":
            await event.respond(
                await self._status_text(int(event.sender_id)),
                buttons=await self._status_page_buttons(),
                parse_mode="md",
            )
        elif command == "jobs":
            await self._respond_jobs(event, int(event.sender_id))
        elif command == "job":
            await self._respond_job(event, int(event.sender_id), argument.strip() or None)
        elif command == "plan":
            await self._respond_plan(event, int(event.sender_id), argument.strip() or None)
        elif command == "stats":
            await event.respond(
                await self._stats_text(int(event.sender_id)),
                buttons=self._more_buttons(),
                parse_mode="md",
            )
        elif command == "health":
            await event.respond(
                await self._health_text(),
                buttons=self._more_buttons(),
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
                buttons=self._more_buttons(),
                parse_mode="md",
            )
        elif command == "retry":
            await self._retry_job(event, int(event.sender_id), argument.strip() or None)
        elif command == "cancel":
            await self._cancel_job(event, int(event.sender_id), argument.strip() or None)
        elif command == "pause":
            await self._pause_command(event, int(event.sender_id), argument.strip() or None)
        elif command == "resume":
            await self._resume_command(event, int(event.sender_id), argument.strip() or None)
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

    async def _on_nav_button(self, event) -> None:
        if not self._authorized(event.sender_id):
            return
        owner_id = int(event.sender_id)
        action = (event.raw_text or "").strip()
        if action == NAV_HOME:
            await event.respond(
                self._home_text(),
                buttons=self._reply_keyboard(),
                parse_mode="md",
            )
        elif action == NAV_JOBS:
            await self._respond_jobs(event, owner_id)
        elif action == NAV_STATUS:
            await event.respond(
                await self._status_text(owner_id),
                buttons=await self._status_page_buttons(),
                parse_mode="md",
            )
        elif action == NAV_ARCHIVE:
            await self._respond_archive(event, owner_id)
        elif action == NAV_CACHE:
            await event.respond(
                await self._cache_text(),
                buttons=self._cache_buttons(),
                parse_mode="md",
            )
        elif action == NAV_MORE:
            await event.respond(
                self._more_text(),
                buttons=self._more_buttons(),
                parse_mode="md",
            )
        raise events.StopPropagation

    async def _on_callback(self, event) -> None:
        if not self._authorized(event.sender_id):
            await self._safe_answer(event, "无权限", alert=True)
            return
        action = bytes(event.data or b"").decode("utf-8", "replace")
        owner_id = int(event.sender_id)
        if action.startswith("ui:fixture-confirm:"):
            job_id = action.split(":", 2)[2]
            await self._confirm_fixture_publish(event, owner_id, job_id)
            return
        if action == "ui:fixture-cancel":
            await self._edit_page(event, "已取消受控发布。", self._nav_buttons())
            return

        if action == "ui:home":
            await self._edit_page(event, self._home_text(), self._home_buttons())
            return
        if action == "ui:status":
            await self._edit_page(
                event,
                await self._status_text(owner_id),
                await self._status_page_buttons(),
            )
            return
        if action == "ui:queue-pause":
            await self._edit_page(
                event,
                "**确认暂停队列**\n\n只会阻止新的 prepare / publish / archive claim；已经在执行的外部操作会在原安全边界结束，不会被强制中断。",
                [[Button.inline("⏸ 确认暂停", b"ui:queue-pause-confirm"), Button.inline("返回", b"ui:status")]],
            )
            return
        if action == "ui:queue-pause-confirm":
            await self._pause_queue_exact(owner_id=owner_id)
            await self._edit_page(
                event,
                await self._status_text(owner_id),
                await self._status_page_buttons(),
            )
            return
        if action == "ui:queue-resume":
            await self._resume_queue_exact()
            await self._edit_page(
                event,
                await self._status_text(owner_id),
                await self._status_page_buttons(),
            )
            return
        if action == "ui:jobs":
            text, buttons = await self._jobs_page(owner_id)
            await self._edit_page(event, text, buttons)
            return
        if action.startswith("ui:jobs:"):
            parts = action.split(":")
            if len(parts) != 4:
                await self._safe_answer(event, "任务列表操作已过期")
                return
            try:
                job_filter = JobListFilter(parts[2])
                page = max(0, int(parts[3]))
            except (ValueError, TypeError):
                await self._safe_answer(event, "任务列表操作已过期")
                return
            text, buttons = await self._jobs_page(owner_id, filter=job_filter, page=page)
            await self._edit_page(event, text, buttons)
            return
        if action.startswith("ui:failures:"):
            parts = action.split(":")
            if len(parts) != 3:
                await self._safe_answer(event, "失败中心操作已过期")
                return
            try:
                page = max(0, int(parts[2]))
            except (ValueError, TypeError):
                await self._safe_answer(event, "失败中心操作已过期")
                return
            text, buttons = await self._failures_page(owner_id, page=page)
            await self._edit_page(event, text, buttons)
            return
        if action == "ui:job":
            job = await self._resolve_job(owner_id, None)
            if job is None:
                await self._edit_page(
                    event,
                    "**任务详情**\n\n还没有任务。直接发送媒体即可开始。",
                    self._nav_buttons(),
                )
            else:
                await self._show_job_callback(event, owner_id, job.id)
            return
        if action == "ui:plan":
            job = await self._resolve_job(owner_id, None)
            if job is None:
                await self._edit_page(
                    event,
                    "**发布计划**\n\n还没有任务。直接发送媒体即可开始。",
                    self._nav_buttons(),
                )
            else:
                await self._show_plan_callback(event, owner_id, job.id)
            return
        if action == "ui:more":
            await self._edit_page(event, self._more_text(), self._more_buttons())
            return
        if action == "ui:stats":
            await self._edit_page(
                event,
                await self._stats_text(owner_id),
                self._more_buttons(),
            )
            return
        if action == "ui:health":
            await self._edit_page(event, await self._health_text(), self._more_buttons())
            return
        if action == "ui:diag":
            await self._edit_page(event, await self._diag_text(), self._more_buttons())
            return
        if action == "ui:archive":
            text, buttons = await self._archive_page(owner_id)
            await self._edit_page(event, text, buttons)
            return
        if action == "ui:cache":
            await self._edit_page(event, await self._cache_text(), self._cache_buttons())
            return
        if action == "ui:help":
            await self._edit_page(event, self._help_text(), self._more_buttons())
            return

        if action == "ui:cache-clean":
            confirmation_data = b"ui:cache-clean-confirm"
            if self._operation_tokens is not None and self._cache_operator is not None:
                stats = await self._cache_operator.stats()
                payload = {
                    "scope": "managed_terminal_cache",
                    "eligible_jobs": int(stats.eligible_jobs),
                    "blocked_by_archive": int(stats.blocked_by_archive),
                    "bytes_used": int(stats.bytes_used),
                }
                operation = await self._operation_tokens.issue(
                    owner_id=owner_id,
                    action="cache_cleanup",
                    resource_type="cache",
                    resource_id="managed",
                    expected_revision=int(stats.eligible_jobs),
                    payload=payload,
                )
                confirmation_data = self._callback_data("cache-clean-confirm", operation.token)
            await self._edit_page(
                event,
                "**确认清理缓存**\n\n只会删除已完成或已取消任务的缓存；失败任务和归档未完成任务不会被删除。",
                [
                    [
                        Button.inline("⚠️ 确认清理", confirmation_data),
                        Button.inline("返回", b"ui:cache"),
                    ]
                ],
            )
            return
        if action == "ui:cache-clean-confirm":
            await self._run_cache_cleanup_callback(event, owner_id=owner_id)
            return
        if action.startswith("ui:cache-clean-confirm:"):
            await self._run_cache_cleanup_callback(
                event,
                owner_id=owner_id,
                token=action[len("ui:cache-clean-confirm:") :],
            )
            return
        if action == "ui:archive-probe":
            await self._edit_page(
                event,
                (
                    "**确认检测 WebDAV**\n\n"
                    "检测会访问 WebDAV；若服务器没有声明完整能力，系统会写入并立即清理一个极小的测试文件。"
                ),
                [
                    [
                        Button.inline("确认检测", b"ui:archive-probe-confirm"),
                        Button.inline("返回", b"ui:archive"),
                    ]
                ],
            )
            return
        if action == "ui:archive-probe-confirm":
            await self._run_archive_probe_callback(event, owner_id)
            return

        job_actions = (
            ("ui:undo-confirm:", self._run_undo_callback),
            ("ui:undo:", self._confirm_undo_callback),
            ("ui:archive-retry-confirm:", self._run_archive_retry_callback),
            ("ui:archive-retry:", self._confirm_archive_retry_callback),
            ("ui:retry-confirm:", self._run_retry_callback),
            ("ui:retry:", self._confirm_retry_callback),
            ("ui:cancel-confirm:", self._run_cancel_callback),
            ("ui:cancel:", self._confirm_cancel_callback),
            ("ui:resume:", self._resume_job_callback),
            ("ui:hold:", self._hold_job_callback),
            ("ui:job-deep:", self._show_deep_job_callback),
            ("ui:plan:", self._show_plan_callback),
            ("ui:job:", self._show_job_callback),
        )
        for prefix, handler in job_actions:
            if action.startswith(prefix):
                await handler(event, owner_id, action[len(prefix) :])
                return

        await self._safe_answer(event, "未知操作")

    async def _respond_jobs(self, event, owner_id: int) -> None:
        text, buttons = await self._jobs_page(owner_id)
        await event.respond(text, buttons=buttons, parse_mode="md")

    async def _respond_job(
        self,
        event,
        owner_id: int,
        prefix: str | None,
    ) -> None:
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            await event.respond(
                "**任务详情**\n\n没有找到唯一对应的任务，请从“我的任务”中直接点选。",
                buttons=self._nav_buttons(),
                parse_mode="md",
            )
            return
        await event.respond(
            await self._job_text(owner_id, job.id),
            buttons=await self._job_buttons(job),
            parse_mode="md",
        )

    async def _respond_plan(
        self,
        event,
        owner_id: int,
        prefix: str | None,
    ) -> None:
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            await event.respond(
                "**发布计划**\n\n没有找到唯一对应的任务，请从“我的任务”中直接点选。",
                buttons=self._nav_buttons(),
                parse_mode="md",
            )
            return
        await event.respond(
            await self._plan_text(owner_id, job.id),
            buttons=self._plan_buttons(job),
            parse_mode="md",
        )

    async def _respond_archive(self, event, owner_id: int) -> None:
        text, buttons = await self._archive_page(owner_id)
        await event.respond(text, buttons=buttons, parse_mode="md")

    async def _show_job_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._owned_job(owner_id, job_id)
        if job is None:
            await self._safe_answer(event, "任务不存在或无权限", alert=True)
            return
        await self._edit_page(
            event,
            await self._job_text(owner_id, job.id),
            await self._job_buttons(job),
        )

    async def _show_deep_job_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._owned_job(owner_id, job_id)
        if job is None:
            await self._safe_answer(event, "任务不存在或无权限", alert=True)
            return
        await self._edit_page(
            event,
            await self._job_text(owner_id, job.id, deep=True),
            await self._job_buttons(job, deep=True),
        )

    async def _show_plan_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._owned_job(owner_id, job_id)
        if job is None:
            await self._safe_answer(event, "任务不存在或无权限", alert=True)
            return
        await self._edit_page(
            event,
            await self._plan_text(owner_id, job.id),
            self._plan_buttons(job),
        )

    async def _confirm_retry_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._owned_job(owner_id, job_id)
        if job is None:
            await self._safe_answer(event, "任务不存在或无权限", alert=True)
            return
        if job.state != JobState.FAILED:
            await self._safe_answer(event, "任务状态已变化，请刷新", alert=True)
            return
        issue = describe_job_failure(job.error_code)
        if job.error_code in {"publish_partial", "publish_uncertain"}:
            await self._edit_page(
                event,
                f"**不能自动重试**\n\n{issue.explanation}\n\n下一步：{issue.action}",
                await self._job_buttons(job),
            )
            return
        control_state = await self._repository.get_job_control(job.id)
        payload = {
            "job_id": job.id,
            "state": job.state.value,
            "error_code": job.error_code or "",
            "retry_count": control_state.retry_count,
        }
        confirmation_ref = await self._issue_job_operation(
            job,
            owner_id=owner_id,
            action="retry_job",
            expected_revision=control_state.retry_count,
            payload=payload,
        )
        label = await self._job_label(job)
        await self._edit_page(
            event,
            (
                f"**确认重试{label}**\n\n"
                f"{issue.explanation}\n\n"
                "系统会再次检查已记录的发布结果，只在确认安全时继续。"
            ),
            [
                [
                    Button.inline(
                        "确认安全重试",
                        self._callback_data("retry-confirm", confirmation_ref),
                    ),
                    Button.inline("返回", self._callback_data("job", job.id)),
                ]
            ],
        )

    async def _run_retry_callback(self, event, owner_id: int, reference: str) -> None:
        job, operation = await self._job_from_operation(
            owner_id,
            reference,
            action="retry_job",
        )
        if job is None:
            message = (
                "任务不存在或无权限"
                if self._operation_tokens is None
                else "确认操作已过期，请重新打开任务"
            )
            await self._safe_answer(event, message, alert=True)
            return
        control_state = await self._repository.get_job_control(job.id)
        payload = {
            "job_id": job.id,
            "state": job.state.value,
            "error_code": job.error_code or "",
            "retry_count": control_state.retry_count,
        }
        if not await self._consume_job_operation(
            owner_id,
            reference,
            operation,
            action="retry_job",
            job=job,
            expected_revision=control_state.retry_count,
            payload=payload,
        ):
            await self._safe_answer(event, "确认操作已过期，请重新打开任务", alert=True)
            return
        text = await self._retry_exact(job, chat_id=event.chat_id)
        current = await self._owned_job(owner_id, job.id)
        buttons = await self._job_buttons(current) if current is not None else self._nav_buttons()
        await self._edit_page(event, text, buttons)

    async def _confirm_cancel_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._owned_job(owner_id, job_id)
        if job is None:
            await self._safe_answer(event, "任务不存在或无权限", alert=True)
            return
        if job.terminal:
            await self._safe_answer(event, "任务已经结束，不能取消", alert=True)
            return
        control_state = await self._repository.get_job_control(job.id)
        revision = control_state.retry_count * 1_000_000 + control_state.hold_revision
        payload = {
            "job_id": job.id,
            "state": job.state.value,
            "cancel_requested": control_state.cancel_requested,
            "retry_count": control_state.retry_count,
            "hold_revision": control_state.hold_revision,
        }
        confirmation_ref = await self._issue_job_operation(
            job,
            owner_id=owner_id,
            action="cancel_job",
            expected_revision=revision,
            payload=payload,
        )
        label = await self._job_label(job)
        await self._edit_page(
            event,
            (
                f"**确认取消{label}**\n\n"
                "取消请求会持久保存，任务将在下一个安全边界停止。"
            ),
            [
                [
                    Button.inline(
                        "⚠️ 确认取消",
                        self._callback_data("cancel-confirm", confirmation_ref),
                    ),
                    Button.inline("返回", self._callback_data("job", job.id)),
                ]
            ],
        )

    async def _run_cancel_callback(self, event, owner_id: int, reference: str) -> None:
        job, operation = await self._job_from_operation(
            owner_id,
            reference,
            action="cancel_job",
        )
        if job is None:
            message = (
                "任务不存在或无权限"
                if self._operation_tokens is None
                else "确认操作已过期，请重新打开任务"
            )
            await self._safe_answer(event, message, alert=True)
            return
        control_state = await self._repository.get_job_control(job.id)
        revision = control_state.retry_count * 1_000_000 + control_state.hold_revision
        payload = {
            "job_id": job.id,
            "state": job.state.value,
            "cancel_requested": control_state.cancel_requested,
            "retry_count": control_state.retry_count,
            "hold_revision": control_state.hold_revision,
        }
        if not await self._consume_job_operation(
            owner_id,
            reference,
            operation,
            action="cancel_job",
            job=job,
            expected_revision=revision,
            payload=payload,
        ):
            await self._safe_answer(event, "确认操作已过期，请重新打开任务", alert=True)
            return
        text = await self._cancel_exact(job, owner_id=owner_id)
        current = await self._owned_job(owner_id, job.id)
        buttons = await self._job_buttons(current) if current is not None else self._nav_buttons()
        await self._edit_page(event, text, buttons)

    async def _hold_job_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._owned_job(owner_id, job_id)
        if job is None:
            await self._safe_answer(event, "任务不存在或无权限", alert=True)
            return
        text = await self._hold_exact(job, owner_id=owner_id)
        current = await self._owned_job(owner_id, job.id)
        buttons = await self._job_buttons(current) if current is not None else self._nav_buttons()
        await self._edit_page(event, text, buttons)

    async def _resume_job_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._owned_job(owner_id, job_id)
        if job is None:
            await self._safe_answer(event, "任务不存在或无权限", alert=True)
            return
        text = await self._resume_exact(job)
        current = await self._owned_job(owner_id, job.id)
        buttons = await self._job_buttons(current) if current is not None else self._nav_buttons()
        await self._edit_page(event, text, buttons)

    async def _confirm_undo_callback(self, event, owner_id: int, job_id: str) -> None:
        if self._undo_service is None:
            await self._safe_answer(event, "撤销服务未启用", alert=True)
            return
        job = await self._owned_job(owner_id, job_id)
        if job is None:
            await self._safe_answer(event, "任务不存在或无权限", alert=True)
            return
        try:
            confirmation = await self._undo_service.prepare(job, owner_id=owner_id)
        except UndoUnavailableError:
            await self._safe_answer(event, "当前没有可撤销的已发布消息", alert=True)
            return
        status = confirmation.status
        label = await self._job_label(job)
        await self._edit_page(
            event,
            (
                f"**⚠️ 确认撤销发布 · {label}**\n\n"
                f"将删除已确认的 Telegram 消息：`{status.remaining_messages}` 条\n"
                f"频道：`{status.remaining_channel_messages}` · 评论区：`{status.remaining_discussion_messages}`\n\n"
                "只删除这个任务已记录的消息；不会删除任务历史或 WebDAV 归档。\n"
                "确认令牌 5 分钟内有效，并且只能使用一次。"
            ),
            [
                [
                    Button.inline(
                        "⚠️ 确认撤销",
                        self._callback_data("undo-confirm", confirmation.operation.token),
                    ),
                    Button.inline("返回", self._callback_data("job", job.id)),
                ]
            ],
        )

    async def _run_undo_callback(self, event, owner_id: int, token: str) -> None:
        if self._undo_service is None:
            await self._safe_answer(event, "撤销服务未启用", alert=True)
            return
        await self._safe_answer(event, "正在撤销已确认的消息…")
        try:
            result = await self._undo_service.confirm(owner_id=owner_id, token=token)
        except UndoOperationInvalidError:
            await self._edit_page(
                event,
                "**撤销操作已过期**\n\n任务状态、已发布消息或确认令牌已经变化。请重新打开任务详情后再操作。",
                self._nav_buttons(),
            )
            return
        job = await self._owned_job(owner_id, result.job_id)
        label = await self._job_label(job) if job is not None else "任务"
        if result.complete:
            text = (
                f"**✅ 撤销完成 · {label}**\n\n"
                f"已删除 `{result.deleted_total}/{result.total_messages}` 条已确认 Telegram 消息。\n"
                "原始发布事实和撤销审计记录仍保留。"
            )
            buttons = (
                await self._job_buttons(job)
                if job is not None
                else self._nav_buttons()
            )
        else:
            text = (
                f"**⚠️ 撤销未完全完成 · {label}**\n\n"
                f"本次删除：`{result.deleted_now}` 条\n"
                f"累计已删除：`{result.deleted_total}/{result.total_messages}` 条\n"
                f"仍需处理：`{result.remaining_messages}` 条\n\n"
                "已成功删除的消息不会再次删除；可继续处理剩余项。"
            )
            buttons = [
                [
                    Button.inline(
                        "↩️ 继续撤销剩余消息",
                        self._callback_data("undo", result.job_id),
                    )
                ],
                [Button.inline("🔎 任务详情", self._callback_data("job", result.job_id))],
            ]
        await self._edit_page(event, text, buttons)

    async def _confirm_archive_retry_callback(
        self,
        event,
        owner_id: int,
        job_id: str,
    ) -> None:
        job = await self._owned_job(owner_id, job_id)
        if job is None:
            await self._safe_answer(event, "任务不存在或无权限", alert=True)
            return
        package = await self._repository.get_archive_package_for_job(job.id)
        if package is None or package.state != ArchivePackageState.FAILED:
            await self._safe_answer(event, "归档状态已变化，请刷新", alert=True)
            return
        issue = describe_archive_failure(package.error_code)
        archive_events = await self._repository.list_archive_events(package.id)
        payload = {
            "job_id": job.id,
            "package_id": package.id,
            "state": package.state.value,
            "error_code": package.error_code or "",
            "event_count": len(archive_events),
        }
        confirmation_ref = await self._issue_job_operation(
            job,
            owner_id=owner_id,
            action="archive_retry",
            expected_revision=len(archive_events),
            payload=payload,
        )
        label = await self._job_label(job)
        await self._edit_page(
            event,
            (
                f"**确认重传归档 · {label}**\n\n"
                f"{issue.explanation}\n\n"
                "已在远端确认的文件会被复用，不会重新发布 Telegram 消息。"
            ),
            [
                [
                    Button.inline(
                        "确认重传归档",
                        self._callback_data("archive-retry-confirm", confirmation_ref),
                    ),
                    Button.inline("返回", self._callback_data("job", job.id)),
                ]
            ],
        )

    async def _run_archive_retry_callback(
        self,
        event,
        owner_id: int,
        reference: str,
    ) -> None:
        job, operation = await self._job_from_operation(
            owner_id,
            reference,
            action="archive_retry",
        )
        if job is None:
            message = (
                "任务不存在或无权限"
                if self._operation_tokens is None
                else "确认操作已过期，请重新打开任务"
            )
            await self._safe_answer(event, message, alert=True)
            return
        package = await self._repository.get_archive_package_for_job(job.id)
        if package is None or package.state != ArchivePackageState.FAILED:
            await self._safe_answer(event, "归档状态已变化，请重新打开任务", alert=True)
            return
        archive_events = await self._repository.list_archive_events(package.id)
        payload = {
            "job_id": job.id,
            "package_id": package.id,
            "state": package.state.value,
            "error_code": package.error_code or "",
            "event_count": len(archive_events),
        }
        if not await self._consume_job_operation(
            owner_id,
            reference,
            operation,
            action="archive_retry",
            job=job,
            expected_revision=len(archive_events),
            payload=payload,
        ):
            await self._safe_answer(event, "确认操作已过期，请重新打开任务", alert=True)
            return
        text = await self._archive_retry_exact(job)
        current = await self._owned_job(owner_id, job.id)
        buttons = await self._job_buttons(current) if current is not None else self._nav_buttons()
        await self._edit_page(event, text, buttons)

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
                stats = await self._cache_operator.stats()
                payload = {
                    "scope": "managed_terminal_cache",
                    "eligible_jobs": int(stats.eligible_jobs),
                    "blocked_by_archive": int(stats.blocked_by_archive),
                    "bytes_used": int(stats.bytes_used),
                }
                await self._operation_tokens.consume(
                    token=token,
                    owner_id=owner_id,
                    action="cache_cleanup",
                    resource_type="cache",
                    resource_id="managed",
                    expected_revision=int(stats.eligible_jobs),
                    payload=payload,
                )
            except OperationTokenInvalidError:
                await self._safe_answer(event, "确认操作已过期，请重新打开缓存页面", alert=True)
                return
        try:
            result = await self._cache_operator.cleanup(force=True)
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

    async def _run_archive_probe_callback(self, event, owner_id: int) -> None:
        if not getattr(self._settings, "archive_enabled", False) or self._archive_operator is None:
            await self._edit_page(event, "🛡️ WebDAV 归档当前未启用。", self._nav_buttons())
            return
        try:
            capabilities = await self._archive_operator.probe()
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "telegram.archive.probe_failed",
                "WebDAV capability probe requested from Telegram failed",
                exception_type=type(exc).__name__,
            )
            await self._edit_page(
                event,
                "❌ WebDAV 连接检测失败。Telegram 发布不受影响，请检查远端服务或稍后重试。",
                (await self._archive_page(owner_id))[1],
            )
            return
        await self._edit_page(
            event,
            (
                "**WebDAV 检测完成**\n\n"
                f"读取目录：`{'支持' if capabilities.supports_propfind else '不支持'}`\n"
                f"创建目录：`{'支持' if capabilities.supports_mkcol else '不支持'}`\n"
                f"上传文件：`{'支持' if capabilities.supports_put else '不支持'}`\n"
                f"读取文件：`{'支持' if capabilities.supports_get else '不支持'}`\n"
                f"原子移动：`{'支持' if capabilities.supports_move else '不支持'}`\n"
                f"完整性标识：`{'支持' if capabilities.supports_etag else '不支持'}`"
            ),
            (await self._archive_page(owner_id))[1],
        )

    async def _retry_exact(self, job: Job, *, chat_id: int) -> str:
        if self._control is None:
            return "任务控制服务未启用。"
        try:
            decision = await self._control.retry_failed(job)
        except ValueError:
            return "🛡️ 任务状态已变化，请刷新任务详情后再操作。"
        except UnsafeRetryError:
            return "🛡️ 系统检测到可能已经发布的 Telegram 消息，已阻止自动重试以免重复。"

        if decision.target_state == JobState.RECEIVED and self._schedule_job is not None:
            self._schedule_job(decision.job, chat_id=chat_id)
            suffix = "已重新进入下载和分析流程。"
        elif (
            decision.target_state == JobState.PLANNED
            and self._settings.publish_enabled
            and self._schedule_job is not None
        ):
            self._schedule_job(decision.job, chat_id=chat_id)
            suffix = "已从确认无副作用的失败步骤继续发布。"
        else:
            suffix = "已恢复到等待发布；当前自动发布关闭，不会产生频道消息。"
        return (
            f"🔁 {await self._job_label(job)} 已开始第 `{decision.retry_count}` 次安全重试。\n"
            f"{suffix}"
        )

    async def _cancel_exact(self, job: Job, *, owner_id: int) -> str:
        if self._control is None:
            return "任务控制服务未启用。"
        try:
            current = await self._control.request_cancel(
                job,
                reason=f"requested by owner {owner_id}",
            )
        except ValueError:
            return "🛡️ 任务已经结束或状态已变化，无法再取消。"
        label = await self._job_label(job)
        if current.state == JobState.CANCELLED:
            return f"⛔ {label} 已取消。"
        return f"⏳ {label} 已记录取消请求，将在下一个安全边界停止。"

    async def _archive_retry_exact(self, job: Job) -> str:
        if not getattr(self._settings, "archive_enabled", False) or self._archive_operator is None:
            return "🛡️ WebDAV 归档当前未启用。"
        package = await self._repository.get_archive_package_for_job(job.id)
        if package is None:
            return "这个任务没有归档记录。"
        try:
            await self._archive_operator.retry_package(package.id)
        except (KeyError, ValueError):
            return "🛡️ 归档状态已变化，请刷新后再操作。"
        except RuntimeError:
            return "🛡️ 本地原始缓存已经不可用，无法安全重传归档。Telegram 发布不受影响。"
        except Exception as exc:
            log_event(
                self._log,
                logging.ERROR,
                "telegram.archive.retry_failed",
                "Archive retry requested from Telegram failed",
                job_id=job.id,
                package_id=package.id,
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            return "❌ 归档重传没有启动。Telegram 发布不受影响，请稍后再试。"
        return (
            f"🔁 {await self._job_label(job)} 的归档已重新排队。\n"
            "系统会复用远端已确认文件，并从未完成的位置继续。"
        )

    async def _job_label(self, job: Job) -> str:
        get_order = getattr(self._repository, "get_accepted_order", None)
        accepted_order = await get_order(job.id) if callable(get_order) else None
        return self._job_number(accepted_order)

    async def _issue_job_operation(
        self,
        job: Job,
        *,
        owner_id: int,
        action: str,
        expected_revision: int,
        payload: dict[str, object],
    ) -> str:
        if self._operation_tokens is None:
            return job.id
        operation = await self._operation_tokens.issue(
            owner_id=owner_id,
            action=action,
            resource_type="job",
            resource_id=job.id,
            expected_revision=expected_revision,
            payload=payload,
        )
        return operation.token

    async def _job_from_operation(
        self,
        owner_id: int,
        reference: str,
        *,
        action: str,
    ) -> tuple[Job | None, object | None]:
        if self._operation_tokens is None:
            return await self._owned_job(owner_id, reference), None
        try:
            operation = await self._operation_tokens.inspect(
                token=reference,
                owner_id=owner_id,
                action=action,
            )
        except OperationTokenInvalidError:
            return None, None
        if operation.resource_type != "job":
            return None, None
        return await self._owned_job(owner_id, operation.resource_id), operation

    async def _consume_job_operation(
        self,
        owner_id: int,
        reference: str,
        operation: object | None,
        *,
        action: str,
        job: Job,
        expected_revision: int,
        payload: dict[str, object],
    ) -> bool:
        if self._operation_tokens is None:
            return True
        if operation is None:
            return False
        try:
            await self._operation_tokens.consume(
                token=reference,
                owner_id=owner_id,
                action=action,
                resource_type="job",
                resource_id=job.id,
                expected_revision=expected_revision,
                payload=payload,
            )
        except OperationTokenInvalidError:
            return False
        return True

    async def _owned_job(self, owner_id: int, job_id: str) -> Job | None:
        if not job_id or len(job_id) > 40:
            return None
        job = await self._repository.get(job_id)
        if job is None or int(job.owner_id) != int(owner_id):
            return None
        return job

    async def _edit_page(self, event, text: str, buttons) -> None:
        try:
            await event.edit(text, buttons=buttons, parse_mode="md")
        except Exception as exc:
            if type(exc).__name__ == "MessageNotModifiedError":
                await self._safe_answer(event, "已经是最新页面")
                return
            log_event(
                self._log,
                logging.WARNING,
                "telegram.ui.edit_failed",
                "Telegram UI page edit failed",
                exception_type=type(exc).__name__,
            )
            await self._safe_answer(event, "页面刷新失败，请稍后重试", alert=True)
            return
        await self._safe_answer(event)

    async def _safe_answer(self, event, text: str | None = None, **kwargs) -> None:
        try:
            await event.answer(text, **kwargs)
        except Exception as exc:
            log_event(
                self._log,
                logging.DEBUG,
                "telegram.ui.callback_answer_failed",
                "Telegram callback acknowledgement failed",
                exception_type=type(exc).__name__,
            )

    def _home_text(self) -> str:
        publish_status = "开启" if getattr(self._settings, "publish_enabled", False) else "关闭"
        return (
            "**TGVIO 首页**\n\n"
            "📥 直接发送图片、视频、文件或媒体组即可开始。\n"
            f"🔗 URL 下载：`{'开启' if self._settings.url_enabled else '关闭'}`。\n"
            f"☁️ WebDAV 归档：`{'开启' if self._settings.archive_enabled else '关闭'}`。\n"
            f"🚦 自动发布：`{publish_status}`。\n\n"
            "手机端直接使用输入框上方的常驻按钮，不需要复制任务 ID 或输入长命令。"
        )

    def _help_text(self) -> str:
        text = (
            "**TGVIO 使用说明**\n\n"
            "1. 直接发送图片、视频、文件、媒体组或支持的链接。\n"
            "2. 点“📋 我的任务”按状态筛选和翻页；“失败中心”只显示需要人工处理的最终失败。\n"
            "3. 点任务编号查看详情；活动任务可暂停/恢复或取消，普通失败可安全重试。\n"
            "4. `/pause` / `/resume` 无参数时控制整个队列；带任务 ID 时只控制该任务。\n"
            "5. WebDAV 失败只影响归档副本，不影响已经完成的 Telegram 发布。\n\n"
            "常用入口都在常驻键盘和页面按钮中，命令菜单也提供合集、队列与任务控制快捷入口。"
        )
        if self._settings.url_enabled:
            text += "\n🔗 也可直接发送一个 HTTP(S) 媒体/站点链接，由 yt-dlp 下载后进入同一流水线。"
        if getattr(self._settings, "live_fixture_enabled", False):
            text += (
                "\n\n🧪 受控发布已启用：`/publish #任务序号`，例如 `/publish #24`。"
                "该命令需要二次确认，且只允许小型安全 fixture。"
            )
        return text

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

    async def _archive_text(self, owner_id: int) -> str:
        enabled = bool(getattr(self._settings, "archive_enabled", False))
        lines = [
            "**WebDAV 归档**",
            "",
            f"状态：`{'开启' if enabled else '关闭'}`",
            f"远端根目录：`{getattr(self._settings, 'archive_remote_root', 'TGVIO')}`",
        ]
        counts = await self._repository.count_archive_packages_by_state(owner_id=owner_id)
        if counts:
            lines.extend(["", "**归档统计**"])
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
                job = await self._repository.get(package.job_id)
                label = await self._job_label(job) if job is not None else "任务"
                lines.append(
                    f"• {label} · {ARCHIVE_STATE_LABELS[package.state]} · "
                    f"`{stored}/{len(package.objects)}` 文件 · {self._human_bytes(total_bytes)}"
                )
                if package.state == ArchivePackageState.FAILED:
                    issue = describe_archive_failure(package.error_code)
                    if job is not None and archive_failure_waits_for_recovery(job, package):
                        recovery = archive_recovery_state(job)
                        if recovery.get("status") == "scheduled":
                            lines.append(
                                f"  ↳ 系统将自动续传第 {recovery.get('next_attempt', '?')}/"
                                f"{recovery.get('max_attempts', '?')} 次，无需操作。"
                            )
                        else:
                            lines.append("  ↳ 系统正在自动判断续传方式，无需操作。")
                    else:
                        lines.append(f"  ↳ {issue.explanation}")
        lines.extend(
            [
                "",
                "Telegram 发布与 WebDAV 归档相互独立：归档失败不会撤回已发布内容。",
                "归档未完成时本地缓存会保留；重传会复用远端已确认文件。",
            ]
        )
        if enabled:
            lines.append("新任务失败时会先自动续传；自动处理停止后仍可点按钮手动重传。")
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
                log_event(
                    self._log,
                    logging.WARNING,
                    "telegram.archive.probe_failed",
                    "WebDAV capability probe requested by command failed",
                    exception_type=type(exc).__name__,
                )
                await event.respond(
                    "❌ WebDAV 连接检测失败。Telegram 发布不受影响，请检查远端服务或稍后重试。"
                )
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
                "用法：`/archive`、`/archive probe` 或 `/archive retry #任务序号`",
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
        await event.respond(await self._archive_retry_exact(job), parse_mode="md")

    async def _load_jobs_page(
        self,
        owner_id: int,
        *,
        filter: JobListFilter,
        page: int,
        page_size: int = 5,
    ) -> JobPage:
        page_jobs = getattr(self._repository, "page_jobs", None)
        if callable(page_jobs):
            return await page_jobs(
                owner_id=owner_id,
                filter=filter,
                page=page,
                page_size=page_size,
            )

        # Compatibility for narrow test doubles. Production SQLite always uses
        # the SQL-paged repository method above.
        jobs = await self._repository.list_recent(owner_id=owner_id, limit=50)
        entries: list[JobListEntry] = []
        for job in jobs:
            control = None
            get_control = getattr(self._repository, "get_job_control", None)
            if callable(get_control):
                control = await get_control(job.id)
            held = bool(getattr(control, "hold_requested", False)) and not job.terminal
            if filter == JobListFilter.ACTIVE and (job.terminal or held):
                continue
            if filter == JobListFilter.HELD and not held:
                continue
            if filter == JobListFilter.FAILED and job.state != JobState.FAILED:
                continue
            if filter == JobListFilter.COMPLETED and job.state not in {
                JobState.SUCCEEDED,
                JobState.CANCELLED,
            }:
                continue
            get_order = getattr(self._repository, "get_accepted_order", None)
            accepted_order = await get_order(job.id) if callable(get_order) else None
            entries.append(
                JobListEntry(
                    job=job,
                    held=held,
                    accepted_order=accepted_order,
                )
            )
        total = len(entries)
        page_size = max(1, int(page_size))
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(max(0, int(page)), total_pages - 1)
        start = current_page * page_size
        return JobPage(
            entries=tuple(entries[start : start + page_size]),
            filter=filter,
            page=current_page,
            page_size=page_size,
            total=total,
        )

    async def _jobs_page(
        self,
        owner_id: int,
        *,
        filter: JobListFilter = JobListFilter.ALL,
        page: int = 0,
    ) -> tuple[str, list]:
        result = await self._load_jobs_page(owner_id, filter=filter, page=page)
        label = JOB_FILTER_LABELS[result.filter]
        lines = [
            f"**我的任务 · {label} · 第 {result.page + 1}/{result.total_pages} 页**",
            f"共 `{result.total}` 个任务",
            "",
        ]
        if not result.entries:
            lines.append("这个筛选下暂时没有任务。")
        else:
            for position, entry in enumerate(result.entries, start=1):
                job = entry.job
                size = sum(item.size_bytes for item in job.items)
                state_label = "已暂停" if entry.held else STATE_LABELS[job.state]
                icon = "⏸" if entry.held else self._job_state_icon(job.state)
                lines.append(
                    f"{position}. {icon} {self._job_number(entry.accepted_order)} · "
                    f"{state_label} · {self._job_local_time(job)}"
                )
                lines.append(
                    f"  {self._job_content_summary(job)} · "
                    f"{self._job_media_counts(job)} · {self._human_bytes(size)}"
                )
                if job.state == JobState.FAILED:
                    lines.append(f"  ⚠️ {describe_job_failure(job.error_code).title}")
                progress = await self._repository.get_job_progress(job.id)
                if progress is not None and not job.terminal and not entry.held:
                    lines.append(f"  {self._progress_text(progress)}")
        lines.extend(["", "点编号查看详情；筛选和翻页都只读取当前 SQL 页面。"])

        rows: list[list] = []
        for position, entry in enumerate(result.entries, start=1):
            rows.append(
                [
                    Button.inline(
                        (
                            f"{position} {self._job_state_icon(entry.job.state, held=entry.held)} "
                            + (
                                f"任务 #{entry.accepted_order}"
                                if entry.accepted_order is not None
                                else "查看任务"
                            )
                        ),
                        self._callback_data("job", entry.job.id),
                    )
                ]
            )
        filter_buttons = [
            Button.inline(
                ("• " if item == result.filter else "") + JOB_FILTER_LABELS[item],
                f"ui:jobs:{item.value}:0".encode("utf-8"),
            )
            for item in JobListFilter
        ]
        rows.append(filter_buttons[:3])
        rows.append(filter_buttons[3:])
        if result.total_pages > 1:
            nav = []
            if result.page > 0:
                nav.append(
                    Button.inline(
                        "⬅️ 上一页",
                        f"ui:jobs:{result.filter.value}:{result.page - 1}".encode("utf-8"),
                    )
                )
            nav.append(
                Button.inline(
                    "🔄 刷新",
                    f"ui:jobs:{result.filter.value}:{result.page}".encode("utf-8"),
                )
            )
            if result.page + 1 < result.total_pages:
                nav.append(
                    Button.inline(
                        "下一页 ➡️",
                        f"ui:jobs:{result.filter.value}:{result.page + 1}".encode("utf-8"),
                    )
                )
            rows.append(nav)
        else:
            rows.append(
                [
                    Button.inline(
                        "🔄 刷新",
                        f"ui:jobs:{result.filter.value}:{result.page}".encode("utf-8"),
                    )
                ]
            )
        rows.append([Button.inline("❌ 失败中心", b"ui:failures:0")])
        rows.extend(self._nav_buttons())
        return "\n".join(lines), rows

    async def _load_failure_page(
        self,
        owner_id: int,
        *,
        page: int,
        page_size: int = 5,
    ) -> FailurePage:
        page_failures = getattr(self._repository, "page_failures", None)
        if callable(page_failures):
            return await page_failures(owner_id=owner_id, page=page, page_size=page_size)

        jobs = await self._repository.list_recent(owner_id=owner_id, limit=50)
        entries: list[FailureSummary] = []
        for job in jobs:
            job_actionable = job.state == JobState.FAILED and not job_failure_waits_for_recovery(job)
            archive = await self._repository.get_archive_package_for_job(job.id)
            archive_actionable = (
                archive is not None
                and archive.state == ArchivePackageState.FAILED
                and not archive_failure_waits_for_recovery(job, archive)
            )
            if not job_actionable and not archive_actionable:
                continue
            get_order = getattr(self._repository, "get_accepted_order", None)
            accepted_order = await get_order(job.id) if callable(get_order) else None
            entries.append(
                FailureSummary(
                    job=job,
                    job_actionable=job_actionable,
                    archive_actionable=archive_actionable,
                    archive_package_id=archive.id if archive is not None else None,
                    archive_error_code=archive.error_code if archive is not None else None,
                    accepted_order=accepted_order,
                )
            )
        total = len(entries)
        page_size = max(1, int(page_size))
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(max(0, int(page)), total_pages - 1)
        start = current_page * page_size
        return FailurePage(
            entries=tuple(entries[start : start + page_size]),
            page=current_page,
            page_size=page_size,
            total=total,
        )

    async def _failures_page(self, owner_id: int, *, page: int = 0) -> tuple[str, list]:
        result = await self._load_failure_page(owner_id, page=page)
        lines = [
            f"**失败中心 · 第 {result.page + 1}/{result.total_pages} 页**",
            f"需要处理：`{result.total}`",
            "",
        ]
        if not result.entries:
            lines.append("✅ 当前没有需要人工处理的失败。自动恢复中的任务不会在这里重复催促。")
        else:
            for position, entry in enumerate(result.entries, start=1):
                job = entry.job
                size = sum(item.size_bytes for item in job.items)
                lines.append(
                    f"{position}. {self._job_number(entry.accepted_order)} · "
                    f"{self._job_local_time(job)}"
                )
                lines.append(
                    f"  {self._job_content_summary(job)} · "
                    f"{self._job_media_counts(job)} · {self._human_bytes(size)}"
                )
                if entry.job_actionable:
                    issue = describe_job_failure(job.error_code)
                    lines.append(f"  ❌ Telegram/任务：{issue.title} · {issue.action}")
                if entry.archive_actionable:
                    issue = describe_archive_failure(entry.archive_error_code)
                    lines.append(f"  ☁️ WebDAV：{issue.title} · {issue.action}")
                if job.error_code in {"publish_partial", "publish_uncertain"}:
                    lines.append("  🛡️ 可能已有可见消息，禁止自动重发。")
        rows: list[list] = []
        for position, entry in enumerate(result.entries, start=1):
            rows.append(
                [
                    Button.inline(
                        (
                            f"{position} 🔎 任务 #{entry.accepted_order}"
                            if entry.accepted_order is not None
                            else f"{position} 🔎 查看任务"
                        ),
                        self._callback_data("job", entry.job.id),
                    )
                ]
            )
        if result.total_pages > 1:
            nav = []
            if result.page > 0:
                nav.append(Button.inline("⬅️ 上一页", f"ui:failures:{result.page - 1}".encode()))
            nav.append(Button.inline("🔄 刷新", f"ui:failures:{result.page}".encode()))
            if result.page + 1 < result.total_pages:
                nav.append(Button.inline("下一页 ➡️", f"ui:failures:{result.page + 1}".encode()))
            rows.append(nav)
        else:
            rows.append([Button.inline("🔄 刷新", b"ui:failures:0")])
        rows.append([Button.inline("← 我的任务", b"ui:jobs"), Button.inline("🏠 首页", b"ui:home")])
        return "\n".join(lines), rows

    @staticmethod
    def _job_state_icon(state: JobState, *, held: bool = False) -> str:
        if held:
            return "⏸"
        return {
            JobState.SUCCEEDED: "✅",
            JobState.FAILED: "❌",
            JobState.CANCELLED: "⛔",
            JobState.DOWNLOADING: "⬇️",
            JobState.PUBLISHING: "📤",
        }.get(state, "⏳")

    async def _archive_page(self, owner_id: int) -> tuple[str, list]:
        text = await self._archive_text(owner_id)
        rows: list[list] = []
        if getattr(self._settings, "archive_enabled", False) and self._archive_operator is not None:
            recent = await self._repository.list_recent_archive_packages(
                owner_id=owner_id,
                limit=8,
            )
            failed = []
            for package in recent:
                if package.state != ArchivePackageState.FAILED:
                    continue
                job = await self._repository.get(package.job_id)
                if job is not None and archive_failure_waits_for_recovery(job, package):
                    continue
                failed.append(package)
            for position, package in enumerate(failed, start=1):
                job = await self._repository.get(package.job_id)
                label = await self._job_label(job) if job is not None else f"失败归档 {position}"
                rows.append(
                    [
                        Button.inline(
                            f"🔁 重传 {label}",
                            self._callback_data("archive-retry", package.job_id),
                        )
                    ]
                )
            rows.append([Button.inline("🔌 检测连接", b"ui:archive-probe")])
        rows.extend(self._nav_buttons())
        return text, rows

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
        get_order = getattr(self._repository, "get_accepted_order", None)
        accepted_order = await get_order(job.id) if callable(get_order) else None
        text = self._render_job_diagnostic(
            snapshot,
            deep=deep,
            accepted_order=accepted_order,
        )
        if self._undo_service is not None and job.terminal:
            undo_status = await self._undo_service.status(job)
            if undo_status.total_messages > 0:
                if undo_status.complete:
                    text += (
                        f"\n\n↩️ **发布撤销** · 已删除 `{undo_status.deleted_messages}/"
                        f"{undo_status.total_messages}` 条已确认 Telegram 消息。"
                    )
                elif undo_status.deleted_messages or undo_status.failed_messages:
                    text += (
                        f"\n\n↩️ **发布撤销** · 已删除 `{undo_status.deleted_messages}/"
                        f"{undo_status.total_messages}`，仍需处理 `{undo_status.remaining_messages}` 条。"
                    )
        control = await self._repository.get_job_control(job.id)
        if control.hold_requested and not job.terminal:
            text += "\n\n⏸ **任务已暂停** · 当前缓存已保留，恢复后从安全边界继续。"
        return text

    def _render_job_diagnostic(
        self,
        snapshot: JobDiagnosticSnapshot,
        *,
        deep: bool,
        accepted_order: int | None = None,
    ) -> str:
        job = snapshot.job
        size = sum(item.size_bytes for item in job.items)
        event_types = {event.event_type for event in snapshot.events}
        lines = [
            f"**{self._job_number(accepted_order)} · 任务详情**",
            "",
            f"状态：**{STATE_LABELS[job.state]}**",
            f"时间：{self._job_local_time(job)}",
            f"媒体：{self._job_media_counts(job)} · `{self._human_bytes(size)}`",
            self._job_content_summary(job),
        ]
        if deep:
            lines.append(f"内部 Job ID：`{job.id}`")
            if job.created_at:
                lines.append(f"创建（UTC）：`{job.created_at}`")
            if job.updated_at:
                lines.append(f"更新（UTC）：`{job.updated_at}`")
        if job.error_code:
            issue = describe_job_failure(job.error_code)
            lines.extend([f"原因：**{issue.title}**", issue.explanation])
            recovery_hint = self._job_recovery_hint(job)
            lines.append(recovery_hint or f"下一步：{issue.action}")
            if deep:
                lines.append(f"内部错误码：`{job.error_code}`")
        if snapshot.progress is not None and not job.terminal:
            lines.append(self._progress_text(snapshot.progress))

        lines.extend(
            [
                "",
                "**处理流程**",
                f"{self._phase_status(event_types, 'job_created', None)} 接收",
                f"{self._phase_status(event_types, 'download_completed', 'download_failed', 'download_started')} 下载",
                f"{self._phase_status(event_types, 'analysis_completed', 'analysis_failed', 'analysis_started')} 分析",
                f"{'✅' if snapshot.plan is not None else '▫️'} 发布计划",
                f"{self._publish_status(snapshot)} Telegram 发布",
                f"{self._archive_status(snapshot)} WebDAV 归档",
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
                    "**Telegram 发布**",
                    f"计划步骤：`{succeeded}/{len(snapshot.plan.steps)}` · 已确认消息记录：`{len(external_effects)}`",
                ]
            )
            for step in failed[:3] if deep else ():
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
                    "**WebDAV 归档**",
                    f"状态：{ARCHIVE_STATE_LABELS[archive.state]} · 文件：`{stored}/{len(archive.objects)}` · `{self._human_bytes(sum(obj.size_bytes for obj in archive.objects))}`",
                ]
            )
            if archive.state == ArchivePackageState.FAILED:
                issue = describe_archive_failure(archive.error_code)
                recovery = archive_recovery_state(job)
                status = str(recovery.get("status", ""))
                if status == "scheduled":
                    lines.extend(
                        [
                            issue.explanation,
                            f"系统将自动进行第 `{recovery.get('next_attempt', '?')}/{recovery.get('max_attempts', '?')}` 次续传，无需操作。",
                        ]
                    )
                elif status in {"exhausted", "abandoned"}:
                    lines.extend(
                        [
                            issue.explanation,
                            "自动续传已停止；Telegram 发布不受影响，仍可手动重传。",
                        ]
                    )
                elif archive_failure_waits_for_recovery(job, archive):
                    lines.extend([issue.explanation, "系统正在自动判断续传方式，无需操作。"])
                else:
                    lines.extend([issue.explanation, f"下一步：{issue.action}"])
            if deep:
                lines.append(
                    f"Package：`{archive.id[-10:]}` · events `{snapshot.archive_event_count}`"
                )
            if archive.error_code and deep:
                lines.append(f"内部错误码：`{archive.error_code}`")
            if failed_objects and deep:
                indexes = ", ".join(str(obj.object_index) for obj in failed_objects[:8])
                lines.append(f"失败对象：`{indexes}`")

        if snapshot.hints:
            icons = {
                "critical": "🔴",
                "error": "🔴",
                "warning": "🟡",
                "info": "🟢",
            }
            lines.extend(["", "**处理建议**"])
            for hint in snapshot.hints:
                code = f" `{hint.code}` ·" if deep else ""
                lines.append(f"{icons.get(hint.severity, '•')}{code} {hint.summary}")
                if hint.action:
                    lines.append(f"  ↳ {hint.action}")

        recent_events = snapshot.events[-8:] if deep else ()
        if recent_events:
            lines.extend(["", "**持久化事件**"])
            for event in recent_events:
                timestamp = event.created_at or "?"
                lines.append(f"• `{timestamp}` · `{event.event_type}`")

        if deep:
            lines.extend(["", "**结构化日志**"])
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
            lines.extend(["", "需要排障时可点下方“技术详情”。"])

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
        label = await self._job_label(job)
        if plan is None:
            return (
                f"**{label} · 发布计划**\n\n"
                f"当前状态：{STATE_LABELS[job.state]}\n"
                "该任务还没有生成 PublishPlan。"
            )
        get_order = getattr(self._repository, "get_accepted_order", None)
        accepted_order = await get_order(job.id) if callable(get_order) else None
        return self._render_plan(job, plan, accepted_order=accepted_order)

    async def _resolve_job(self, owner_id: int, prefix: str | None) -> Job | None:
        normalized = prefix.strip().lower() if prefix else ""
        numeric = normalized.removeprefix("#")
        if numeric.isdigit():
            get_by_order = getattr(self._repository, "get_by_accepted_order", None)
            if callable(get_by_order):
                exact = await get_by_order(int(owner_id), int(numeric))
                if exact is not None:
                    return exact
            # A short numeric value is exclusively a human task number. Keep
            # accepting the extraordinarily rare all-numeric 32-char UUID as
            # an exact technical identifier, but never as a prefix fallback.
            if normalized.startswith("#") or len(normalized) != 32:
                return None
        if len(normalized) == 32:
            exact = await self._repository.get(normalized)
            if exact is not None and int(exact.owner_id) == int(owner_id):
                return exact
        jobs = await self._repository.list_recent(owner_id=owner_id, limit=50)
        if not jobs:
            return None
        if not prefix:
            return next((job for job in jobs if job.state in {JobState.PLANNED, JobState.PUBLISHING, JobState.SUCCEEDED}), jobs[0])
        matches = [job for job in jobs if job.id.lower().startswith(normalized)]
        return matches[0] if len(matches) == 1 else None

    async def _pause_command(self, event, owner_id: int, prefix: str | None) -> None:
        if self._control is None:
            await event.respond("任务控制服务未启用。")
            return
        if not prefix:
            await self._pause_queue_exact(owner_id=owner_id)
            await event.respond(
                "⏸ 队列已暂停：不会再取得新的 prepare / publish / archive claim；当前外部操作不会被强制中断。",
                parse_mode="md",
            )
            return
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            await event.respond("没有找到唯一对应的任务。")
            return
        await event.respond(await self._hold_exact(job, owner_id=owner_id), parse_mode="md")

    async def _resume_command(self, event, owner_id: int, prefix: str | None) -> None:
        if self._control is None:
            await event.respond("任务控制服务未启用。")
            return
        if not prefix:
            await self._resume_queue_exact()
            await event.respond("▶️ 队列已恢复，新 claim 可以继续取得。", parse_mode="md")
            return
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            await event.respond("没有找到唯一对应的任务。")
            return
        await event.respond(await self._resume_exact(job), parse_mode="md")

    async def _cancel_job(self, event, owner_id: int, prefix: str | None) -> None:
        if self._control is None:
            await event.respond("任务控制服务未启用。")
            return
        if not prefix:
            await event.respond("请使用 `/cancel #任务序号`，例如 `/cancel #24`。也可直接在任务详情点取消按钮。", parse_mode="md")
            return
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            await event.respond("没有找到唯一对应的任务。")
            return
        await event.respond(
            await self._cancel_exact(job, owner_id=owner_id),
            parse_mode="md",
        )

    async def _hold_exact(self, job: Job, *, owner_id: int) -> str:
        if self._control is None:
            return "任务控制服务未启用。"
        try:
            control = await self._control.request_hold(
                job,
                reason=f"requested by owner {owner_id}",
            )
        except ValueError:
            return "🛡️ 任务已经结束或状态已变化，无法暂停。"
        return (
            f"⏸ {await self._job_label(job)} 已暂停（revision `{control.hold_revision}`）。\n"
            "当前正在执行的安全单元会先结束；后续 prepare / publish / archive claim 不会再取得。"
        )

    async def _resume_exact(self, job: Job) -> str:
        if self._control is None:
            return "任务控制服务未启用。"
        try:
            control = await self._control.resume(job)
        except ValueError:
            return "🛡️ 任务已经结束或状态已变化，无法恢复。"
        if self._schedule_job is not None:
            display = await self._repository.get_job_display_message(job.id)
            if display is not None:
                self._schedule_job(
                    job,
                    chat_id=display.chat_id,
                    status_message_id=display.message_id,
                )
            else:
                self._schedule_job(job)
        return f"▶️ {await self._job_label(job)} 已恢复（revision `{control.hold_revision}`），将从 durable 状态继续。"

    async def _pause_queue_exact(self, *, owner_id: int) -> None:
        if self._control is None:
            return
        await self._control.pause_queue(reason=f"requested by owner {owner_id}")

    async def _resume_queue_exact(self) -> None:
        if self._control is None:
            return
        await self._control.resume_queue()
        if self._schedule_job is None:
            return
        recoverable = await self._repository.list_by_states(
            (
                JobState.RECEIVED,
                JobState.DOWNLOADING,
                JobState.DOWNLOADED,
                JobState.ANALYZING,
                JobState.ANALYZED,
                JobState.PLANNED,
                JobState.PUBLISHING,
            )
        )
        for job in recoverable:
            control = await self._repository.get_job_control(job.id)
            if control.hold_requested:
                continue
            display = await self._repository.get_job_display_message(job.id)
            if display is not None:
                self._schedule_job(
                    job,
                    chat_id=display.chat_id,
                    status_message_id=display.message_id,
                )
            else:
                self._schedule_job(job)

    async def _retry_job(self, event, owner_id: int, prefix: str | None) -> None:
        if self._control is None:
            await event.respond("任务控制服务未启用。")
            return
        if not prefix:
            await event.respond("请使用 `/retry #任务序号`，例如 `/retry #24`。也可直接在任务详情点重试按钮。", parse_mode="md")
            return
        job = await self._resolve_job(owner_id, prefix)
        if job is None:
            await event.respond("没有找到唯一对应的任务。")
            return
        await event.respond(
            await self._retry_exact(job, chat_id=event.chat_id),
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
        confirmation_ref = await self._issue_job_operation(
            job,
            owner_id=owner_id,
            action="fixture_publish",
            expected_revision=int(plan.version),
            payload={
                "job_id": job.id,
                "plan_id": plan.id,
                "plan_version": int(plan.version),
                "state": job.state.value,
            },
        )
        label = await self._job_label(job)
        await event.respond(
            (
                f"**{label} · 受控真实发布确认**\n\n"
                f"媒体：`{len(job.items)}` 项\n"
                f"总大小：`{self._human_bytes(sum(item.size_bytes for item in job.items))}`\n"
                "确认后会对真实目标频道产生 Telegram 消息。"
            ),
            buttons=[
                [
                    Button.inline(
                        "⚠️ 确认真实发布",
                        self._callback_data("fixture-confirm", confirmation_ref),
                    ),
                    Button.inline("取消", b"ui:fixture-cancel"),
                ]
            ],
            parse_mode="md",
        )

    async def _confirm_fixture_publish(self, event, owner_id: int, reference: str) -> None:
        if self._fixture_execution is None or not getattr(
            self._settings, "live_fixture_enabled", False
        ):
            await event.answer("受控发布未启用", alert=True)
            return
        job, operation = await self._job_from_operation(
            owner_id,
            reference,
            action="fixture_publish",
        )
        if job is None:
            message = (
                "任务不存在或无权限"
                if self._operation_tokens is None
                else "确认操作已过期，请重新发起"
            )
            await event.answer(message, alert=True)
            return
        plan = await self._repository.get_publish_plan(job.id)
        if plan is None:
            await event.answer("发布计划不存在", alert=True)
            return
        error = self._fixture_validation_error(job, plan)
        if error:
            await event.answer(error, alert=True)
            return
        payload = {
            "job_id": job.id,
            "plan_id": plan.id,
            "plan_version": int(plan.version),
            "state": job.state.value,
        }
        if not await self._consume_job_operation(
            owner_id,
            reference,
            operation,
            action="fixture_publish",
            job=job,
            expected_revision=int(plan.version),
            payload=payload,
        ):
            await event.answer("确认操作已过期，请重新发起", alert=True)
            return
        await event.answer("受控发布已开始")
        await event.edit(
            f"🧪 {await self._job_label(job)} 正在执行受控真实发布……",
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
                f"❌ {await self._job_label(job)} 受控发布失败：`{type(exc).__name__}`",
                parse_mode="md",
            )
            return
        await self._client.send_message(
            chat_id,
            f"✅ {await self._job_label(completed)} 受控发布完成。",
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

    def _render_plan(
        self,
        job: Job,
        plan: PublishPlan,
        *,
        accepted_order: int | None = None,
    ) -> str:
        summary = plan.summary
        lines = [
            f"**{self._job_number(accepted_order)} · 发布计划 v{plan.version}**",
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
            recovery_hint = TelethonBotUI._job_recovery_hint(job)
            if recovery_hint:
                return recovery_hint
            if job.error_code in {"publish_partial", "publish_uncertain"}:
                return "🛡️ 已检测到可能存在 Telegram 副作用；禁止盲重试，需要人工核对。"
            return "🔁 可在任务详情中点“重试任务”；系统会再次校验发布记录。"
        if job.state in {
            JobState.RECEIVED,
            JobState.DOWNLOADING,
            JobState.DOWNLOADED,
            JobState.ANALYZING,
            JobState.ANALYZED,
            JobState.PLANNED,
            JobState.PUBLISHING,
        }:
            return "⛔ 可在任务详情中点“取消任务”，任务会在安全边界停止。"
        return ""

    @staticmethod
    def _job_recovery_hint(job: Job) -> str:
        recovery = job_recovery_state(job)
        status = str(recovery.get("status", ""))
        if status == "scheduled":
            return (
                f"🔄 系统将自动进行第 `{recovery.get('next_attempt', '?')}/"
                f"{recovery.get('max_attempts', '?')}` 次安全重试，无需操作。"
            )
        if status == "quarantined":
            return "🛡️ 任务已隔离且不会自动重发；后续队列继续，请核对频道实际消息。"
        if status == "manual_review":
            return "🛡️ 安全检查阻止重发；任务已跳过，后续队列继续。"
        if status in {"exhausted", "abandoned"}:
            return (
                f"⏭️ 自动恢复已停止（尝试 `{recovery.get('attempt_count', 0)}` 次）；"
                "任务已跳过，后续队列继续。"
            )
        if job_failure_waits_for_recovery(job):
            return "🔄 系统正在自动判断重试或跳过，无需操作。"
        return ""

    @staticmethod
    def _human_bytes(value: int) -> str:
        amount = float(max(0, value))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
            amount /= 1024
        return f"{amount:.1f}TB"

    @staticmethod
    def _md_text(value: object, *, limit: int = 56) -> str:
        text = " ".join(str(value or "").strip().split())
        if not text:
            return ""
        if len(text) > limit:
            text = text[: max(1, limit - 1)].rstrip() + "…"
        for marker in ("\\", "`", "*", "_", "[", "]"):
            text = text.replace(marker, f"\\{marker}")
        return text

    @staticmethod
    def _job_number(accepted_order: int | None) -> str:
        return f"任务 #{accepted_order}" if accepted_order is not None else "任务"

    @staticmethod
    def _job_local_time(job: Job) -> str:
        raw = str(job.created_at or "").strip()
        if not raw:
            return "时间未知"
        candidate = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                try:
                    parsed = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                return raw[:16]
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        return local.strftime("%m-%d %H:%M")

    @staticmethod
    def _job_media_counts(job: Job) -> str:
        labels = {
            MediaKind.VIDEO: "视频",
            MediaKind.PHOTO: "图片",
            MediaKind.DOCUMENT: "文件",
            MediaKind.TEXT: "文字",
        }
        parts: list[str] = []
        for kind in (MediaKind.VIDEO, MediaKind.PHOTO, MediaKind.DOCUMENT, MediaKind.TEXT):
            count = sum(1 for item in job.items if item.kind == kind)
            if count:
                parts.append(f"{count} 个{labels[kind]}")
        return " · ".join(parts) if parts else f"{len(job.items)} 项"

    def _job_content_summary(self, job: Job) -> str:
        names = [self._md_text(item.name) for item in job.items if item.name]
        names = [name for name in names if name]
        if names:
            suffix = f" 等 {len(job.items)} 项" if len(job.items) > 1 else ""
            return f"🎬 {names[0]}{suffix}"

        captions = [
            self._md_text(item.caption)
            for item in job.items
            if str(item.caption or "").strip()
        ]
        captions = [caption for caption in captions if caption]
        collection_caption = self._md_text(job.policy.get("collection_caption", ""))
        if captions or collection_caption:
            return f"📝 {captions[0] if captions else collection_caption}"

        for item in job.items:
            host = self._md_text(item.metadata.get("url_hostname", ""), limit=42)
            if host:
                return f"🔗 {host}"

        first = job.items[0] if job.items else None
        if first is not None and first.kind == MediaKind.VIDEO:
            details: list[str] = []
            if first.duration_seconds:
                seconds = max(0, int(first.duration_seconds))
                minutes, seconds = divmod(seconds, 60)
                hours, minutes = divmod(minutes, 60)
                duration = (
                    f"{hours}:{minutes:02d}:{seconds:02d}"
                    if hours
                    else f"{minutes}:{seconds:02d}"
                )
                details.append(duration)
            if first.width and first.height:
                details.append(f"{first.width}×{first.height}")
            if details:
                return "🎬 视频 · " + " · ".join(details)
        return "🎬 " + self._job_media_counts(job)

    def _job_identity_lines(
        self,
        job: Job,
        *,
        accepted_order: int | None,
        prefix: str = "",
    ) -> list[str]:
        size = sum(item.size_bytes for item in job.items)
        title = self._job_number(accepted_order)
        if prefix:
            title = f"{prefix}{title}"
        return [
            f"{title} · {self._job_local_time(job)} · {self._job_media_counts(job)} · {self._human_bytes(size)}",
            f"  {self._job_content_summary(job)}",
        ]

    def _home_buttons(self):
        return [
            [Button.inline("📋 我的任务", b"ui:jobs"), Button.inline("📊 状态", b"ui:status")],
            [Button.inline("☁️ 归档", b"ui:archive"), Button.inline("🧹 缓存", b"ui:cache")],
            [Button.inline("ℹ️ 更多", b"ui:more")],
        ]

    def _reply_keyboard(self):
        def text_button(label: str):
            return Button.text(
                label,
                resize=True,
                single_use=False,
                persistent=True,
                placeholder="发送媒体，或选择一个操作",
            )

        return [
            [text_button(COLLECTION_BEGIN_BUTTON), text_button(COLLECTION_END_BUTTON)],
            [text_button(NAV_HOME), text_button(NAV_JOBS)],
            [text_button(NAV_STATUS), text_button(NAV_ARCHIVE)],
            [text_button(NAV_CACHE), text_button(NAV_MORE)],
        ]

    @staticmethod
    def _more_text() -> str:
        return (
            "**更多工具**\n\n"
            "这里是统计、运行健康和脱敏技术诊断。日常转发一般不需要打开这些页面。"
        )

    def _more_buttons(self):
        return [
            [Button.inline("📈 统计", b"ui:stats"), Button.inline("❤️ 运行健康", b"ui:health")],
            [Button.inline("🩺 技术诊断", b"ui:diag"), Button.inline("❓ 使用帮助", b"ui:help")],
            [Button.inline("📋 我的任务", b"ui:jobs"), Button.inline("🏠 首页", b"ui:home")],
        ]

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

    def _nav_buttons(self):
        return [
            [Button.inline("📋 我的任务", b"ui:jobs"), Button.inline("📊 刷新状态", b"ui:status")],
            [Button.inline("🏠 首页", b"ui:home"), Button.inline("ℹ️ 更多", b"ui:more")],
        ]

    def _cache_buttons(self):
        rows = []
        if self._cache_operator is not None:
            rows.append([Button.inline("🧹 清理已完成缓存", b"ui:cache-clean")])
        rows.extend(
            [
                [Button.inline("🔄 刷新", b"ui:cache")],
                [Button.inline("📋 我的任务", b"ui:jobs"), Button.inline("🏠 首页", b"ui:home")],
            ]
        )
        return rows

    async def _job_buttons(self, job: Job, *, deep: bool = False):
        rows = []
        action_row = []
        control_state = await self._repository.get_job_control(job.id)
        if (
            job.state == JobState.FAILED
            and job.error_code not in {"publish_partial", "publish_uncertain"}
            and self._control is not None
            and not job_failure_waits_for_recovery(job)
            and job_recovery_state(job).get("status") != "manual_review"
        ):
            action_row.append(
                Button.inline("🔁 重试任务", self._callback_data("retry", job.id))
            )
        if not job.terminal and self._control is not None:
            if control_state.hold_requested:
                action_row.append(
                    Button.inline("▶️ 恢复任务", self._callback_data("resume", job.id))
                )
            else:
                action_row.append(
                    Button.inline("⏸ 暂停任务", self._callback_data("hold", job.id))
                )
            action_row.append(
                Button.inline("⛔ 取消任务", self._callback_data("cancel", job.id))
            )
        if action_row:
            rows.append(action_row)

        package = await self._repository.get_archive_package_for_job(job.id)
        if (
            package is not None
            and package.state == ArchivePackageState.FAILED
            and getattr(self._settings, "archive_enabled", False)
            and self._archive_operator is not None
            and not archive_failure_waits_for_recovery(job, package)
        ):
            rows.append(
                [
                    Button.inline(
                        "☁️ 重传失败归档",
                        self._callback_data("archive-retry", job.id),
                    )
                ]
            )

        if self._undo_service is not None and job.terminal:
            undo_status = await self._undo_service.status(job)
            if undo_status.remaining_messages > 0:
                rows.append(
                    [
                        Button.inline(
                            "↩️ 撤销发布",
                            self._callback_data("undo", job.id),
                        )
                    ]
                )

        plan = await self._repository.get_publish_plan(job.id)
        detail_row = []
        if plan is not None:
            detail_row.append(
                Button.inline("🧠 发布计划", self._callback_data("plan", job.id))
            )
        if deep:
            detail_row.append(
                Button.inline("简明详情", self._callback_data("job", job.id))
            )
        else:
            detail_row.append(
                Button.inline("🩺 技术详情", self._callback_data("job-deep", job.id))
            )
        if detail_row:
            rows.append(detail_row)
        rows.append(
            [Button.inline("← 我的任务", b"ui:jobs"), Button.inline("🏠 首页", b"ui:home")]
        )
        return rows

    def _plan_buttons(self, job: Job):
        return [
            [Button.inline("🔎 任务详情", self._callback_data("job", job.id))],
            [Button.inline("← 我的任务", b"ui:jobs"), Button.inline("🏠 首页", b"ui:home")],
        ]

    @staticmethod
    def _callback_data(action: str, job_id: str) -> bytes:
        payload = f"ui:{action}:{job_id}".encode("utf-8")
        if len(payload) > 64:
            raise ValueError("Telegram callback data exceeds 64 bytes")
        return payload

    def _authorized(self, sender_id: int | None) -> bool:
        return sender_id is not None and int(sender_id) in self._settings.allowed_users
