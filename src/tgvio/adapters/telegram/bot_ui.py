from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import shutil
from typing import Callable
from zoneinfo import ZoneInfo

from telethon import Button, TelegramClient, events
from telethon.tl import functions, types

from tgvio.adapters.telegram.user_messages import (
    describe_archive_failure,
    describe_job_failure,
)
from tgvio.application.archive_capabilities import (
    ArchiveCapabilityStatus,
    get_archive_capability_status,
)
from tgvio.application.archive_deletion import (
    ArchiveDeletionOperationInvalidError,
    ArchiveDeletionService,
    ArchiveDeletionUnavailableError,
)
from tgvio.application.auto_recovery import (
    archive_failure_waits_for_recovery,
    archive_recovery_state,
    job_failure_waits_for_recovery,
    job_recovery_state,
)
from tgvio.application.diagnostics import DiagnosticSnapshotService
from tgvio.application.execution import PublishExecutionEngine
from tgvio.application.job_diagnostics import JobDiagnosticService, JobDiagnosticSnapshot
from tgvio.application.job_control import JobControlService, UnsafeRetryError
from tgvio.application.operation_tokens import (
    OperationTokenInvalidError,
    OperationTokenService,
)
from tgvio.application.ports import ArchiveOperator, CacheOperator, JobRepository
from tgvio.application.item_recovery import SkippedItemRecoveryService
from tgvio.application.undo import (
    UndoOperationInvalidError,
    UndoService,
    UndoUnavailableError,
)
from tgvio.config import Settings
from tgvio.domain.archive import (
    ArchiveDeletionStatus,
    ArchivePackage,
    ArchivePackageState,
)
from tgvio.domain.diagnostics import DiagnosticSnapshot
from tgvio.domain.job import Job, JobState, MediaKind
from tgvio.domain.job_query import (
    FailurePage,
    FailureSummary,
    JobListEntry,
    JobListFilter,
    JobPage,
)
from tgvio.domain.operations import UndoStatus
from tgvio.domain.publish import PublishPlan, PublishStepKind, PublishStepState, PublishTarget
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event
from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.bot_ui_format import BotUIFormatMixin
from tgvio.adapters.telegram.bot_ui_jobs import BotUIJobsMixin
from tgvio.adapters.telegram.bot_ui_result import BotUIResultMixin
from tgvio.adapters.telegram.bot_ui_drafts import BotUIDraftsMixin
from tgvio.adapters.telegram.bot_ui_styles import BotUIStylesMixin
from tgvio.adapters.telegram.bot_ui_content import BotUIContentMixin
from tgvio.adapters.telegram.bot_ui_source import BotUISourceMixin
from tgvio.adapters.telegram.bot_ui_archive import BotUIArchiveMixin
from tgvio.adapters.telegram.bot_ui_fixture import BotUIFixtureMixin
class TelethonBotUI(
    BotUIFixtureMixin,
    BotUIArchiveMixin,
    BotUIResultMixin,
    BotUIDraftsMixin,
    BotUIStylesMixin,
    BotUIContentMixin,
    BotUISourceMixin,
    BotUIJobsMixin,
    BotUIFormatMixin,
):
    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        repository: JobRepository,
        fixture_execution: PublishExecutionEngine | None = None,
        control: JobControlService | None = None,
        schedule_job: Callable | None = None,
        archive_operator: ArchiveOperator | None = None,
        archive_deletion_service: ArchiveDeletionService | None = None,
        cache_operator: CacheOperator | None = None,
        job_diagnostics: JobDiagnosticService | None = None,
        undo_service: UndoService | None = None,
        operation_tokens: OperationTokenService | None = None,
        diagnostic_service: DiagnosticSnapshotService | None = None,
        runtime_flags: object | None = None,
        intake: object | None = None,
        source_coordinator: object | None = None,
        pick_previews: object | None = None,
        item_recovery: SkippedItemRecoveryService | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._repository = repository
        self._fixture_execution = fixture_execution
        self._control = control
        self._schedule_job = schedule_job
        self._archive_operator = archive_operator
        self._archive_deletion_service = archive_deletion_service
        self._cache_operator = cache_operator
        self._job_diagnostics = job_diagnostics or JobDiagnosticService(repository)
        self._undo_service = undo_service
        self._operation_tokens = operation_tokens
        self._diagnostic_service = diagnostic_service
        self._runtime_flags = runtime_flags
        self._intake = intake
        self._source, self._pick_previews = source_coordinator, pick_previews
        self._item_recovery = item_recovery
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
            text, _buttons = await self._context_home(int(event.sender_id), int(event.chat_id))
            await event.respond(
                text,
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
        elif command == "settings":
            quiet = await self._quiet_enabled(int(event.sender_id))
            await event.respond(
                self._settings_page_text(quiet),
                buttons=self._settings_page_buttons(quiet),
                parse_mode="md",
            )
        elif command == "thumb":
            await self._set_thumbnail_from_message(event, int(event.sender_id))
        elif command == "caption":
            await self._set_caption_template_from_message(
                event,
                int(event.sender_id),
                argument,
            )
        elif command == "source":
            text, buttons = await self._source_page(int(event.sender_id))
            await event.respond(text, buttons=buttons, parse_mode="md")
        elif command == "pick":
            await self._pick_command(event, int(event.sender_id), argument)
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
            text, _buttons = await self._context_home(owner_id, int(event.chat_id))
            await event.respond(
                text,
                buttons=self._reply_keyboard(),
                parse_mode="md",
            )
        elif action == NAV_JOBS:
            await self._respond_jobs(event, owner_id)
        elif action == NAV_HISTORY:
            text, buttons = await self._jobs_page(owner_id, filter=JobListFilter.HISTORY)
            await event.respond(text, buttons=buttons, parse_mode="md")
        elif action == NAV_DRAFTS:
            text, buttons = await self._render_drafts(owner_id, page=0)
            await event.respond(text, buttons=buttons, parse_mode="md")
        elif action == NAV_STYLE:
            text, buttons = await self._render_styles(owner_id)
            await event.respond(text, buttons=buttons, parse_mode="md")
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
            text, buttons = await self._context_home(owner_id, int(event.chat_id))
            await self._edit_page(event, text, buttons)
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
        if action == "ui:styles":
            await self._show_styles_callback(event, owner_id)
            return
        if action == "ui:content":
            await self._show_content_callback(event, owner_id)
            return
        if await self._handle_content_callback(event, owner_id, action):
            return
        if await self._handle_source_callback(event, owner_id, action):
            return
        if action == "ui:settings":
            quiet = await self._quiet_enabled(owner_id)
            await self._edit_page(
                event, self._settings_page_text(quiet), self._settings_page_buttons(quiet)
            )
            return
        if action.startswith("set:"):
            await self._toggle_setting_callback(event, action.split(":", 1)[1])
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
            cleanup_targets: tuple[str, ...] | None = None
            if self._operation_tokens is not None and self._cache_operator is not None:
                cleanup_targets = await self._cache_operator.cleanup_candidates(force=True)
                payload = {
                    "version": 1,
                    "scope": "managed_terminal_cache",
                    "job_ids": list(cleanup_targets),
                }
                operation = await self._operation_tokens.issue(
                    owner_id=owner_id,
                    action="cache_cleanup",
                    resource_type="cache",
                    resource_id="managed",
                    expected_revision=len(cleanup_targets),
                    payload=payload,
                )
                confirmation_data = self._callback_data("cache-clean-confirm", operation.token)
            target_text = (
                f"\n\n本次精确匹配：`{len(cleanup_targets)}` 个任务缓存。"
                if cleanup_targets is not None
                else ""
            )
            await self._edit_page(
                event,
                (
                    "**确认清理缓存**\n\n"
                    "只会删除已完成或已取消任务的缓存；失败任务和归档未完成任务不会被删除。"
                    f"{target_text}"
                ),
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
            ("ui:item-recover-confirm:", self._run_item_recovery_callback),
            ("ui:item-recover:", self._confirm_item_recovery_callback),
            ("ui:undo-confirm:", self._run_undo_callback),
            ("ui:undo:", self._confirm_undo_callback),
            ("ui:archive-delete-confirm:", self._run_archive_delete_callback),
            ("ui:archive-delete:", self._confirm_archive_delete_callback),
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
            ("ui:result:", self._show_result_callback),
            ("ui:link-info:", self._link_info_callback),
            ("ui:share:", self._share_callback),
            ("ui:repost:", self._repost_callback),
            ("ui:restyle:", self._restyle_callback),
            ("ui:favorites:", self._favorites_page_callback),
            ("ui:unfav:", self._favorite_off_callback),
            ("ui:fav:", self._favorite_on_callback),
            ("ui:drafts:", self._show_drafts_callback),
            ("ui:draft-open:", self._draft_open_callback),
            ("ui:draft-del:", self._draft_delete_callback),
            ("ui:style-custom:", self._style_custom_callback),
            ("ui:style-reset", self._style_reset_callback),
            ("ui:style:", self._set_style_callback),
            ("ui:job:", self._show_job_callback),
        )
        for prefix, handler in job_actions:
            if action.startswith(prefix):
                await handler(event, owner_id, action[len(prefix) :])
                return

        await self._safe_answer(event, "未知操作")

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

    async def _toggle_setting_callback(self, event, key: str) -> None:
        owner_id = int(event.sender_id)
        if key == "quiet_mode":
            current = await self._quiet_enabled(owner_id)
            try:
                await self._repository.set_user_quiet_mode(owner_id, not current)
            except Exception:
                await self._safe_answer(event, "保存失败", alert=True)
                return
            await self._safe_answer(event, "已更新")
            quiet = not current
            try:
                await event.edit(
                    self._settings_page_text(quiet),
                    buttons=self._settings_page_buttons(quiet),
                    parse_mode="md",
                )
            except Exception:
                pass
            return
        allowed = {
            "alerts_enabled",
            "collection_preview_enabled",
            "daily_cleanup_enabled",
        }
        if self._runtime_flags is None or key not in allowed:
            await self._safe_answer(event, "设置不可用", alert=True)
            return
        current = self._runtime_flags.bool(key, True)
        try:
            await self._runtime_flags.set(self._repository, key, "false" if current else "true")
        except Exception:
            await self._safe_answer(event, "保存失败", alert=True)
            return
        await self._safe_answer(event, "已更新")
        quiet = await self._quiet_enabled(owner_id)
        try:
            await event.edit(
                self._settings_page_text(quiet),
                buttons=self._settings_page_buttons(quiet),
                parse_mode="md",
            )
        except Exception:
            pass

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

        if (
            package is not None
            and package.state == ArchivePackageState.COMMITTED
            and job.terminal
            and self._archive_deletion_service is not None
        ):
            deletion = await self._safe_archive_deletion_status(package)
            if deletion is None or not deletion.complete:
                rows.append(
                    [
                        Button.inline(
                            (
                                "🧹 继续清理远端归档"
                                if deletion is not None
                                else "🗑 删除远端归档"
                            ),
                            self._callback_data("archive-delete", job.id),
                        )
                    ]
                )

        if self._undo_service is not None and job.terminal:
            undo_status = await self._safe_undo_status(job)
            if undo_status is not None and undo_status.remaining_messages > 0:
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

    @staticmethod
    def _callback_data(action: str, job_id: str) -> bytes:
        payload = f"ui:{action}:{job_id}".encode("utf-8")
        if len(payload) > 64:
            raise ValueError("Telegram callback data exceeds 64 bytes")
        return payload

    def _authorized(self, sender_id: int | None) -> bool:
        return sender_id is not None and int(sender_id) in self._settings.allowed_users
