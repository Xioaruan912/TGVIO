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




class BotUIJobsMixin:
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
        except Exception as exc:
            log_event(
                self._log,
                logging.ERROR,
                "telegram.undo.prepare_failed",
                "Unable to prepare durable publish undo",
                job_id=job.id,
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            await self._safe_answer(event, "暂时无法准备撤销，请稍后重试", alert=True)
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
        except Exception as exc:
            log_event(
                self._log,
                logging.ERROR,
                "telegram.undo.execution_failed",
                "Durable publish undo did not complete",
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            await self._edit_page(
                event,
                (
                    "**撤销暂未完成**\n\n"
                    "系统已保留每条消息的处理记录，不会把失败伪装成成功。\n\n"
                    "请重新打开任务详情，只会继续处理尚未确认删除的消息。"
                ),
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

    async def _job_label(self, job: Job) -> str:
        get_order = getattr(self._repository, "get_accepted_order", None)
        accepted_order = await get_order(job.id) if callable(get_order) else None
        return self._job_number(accepted_order)

    async def _safe_undo_status(self, job: Job) -> UndoStatus | None:
        if self._undo_service is None:
            return None
        try:
            return await self._undo_service.status(job)
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "telegram.undo.status_failed",
                "Unable to read durable publish undo status",
                job_id=job.id,
                exception_type=type(exc).__name__,
            )
            return None

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
            undo_status = await self._safe_undo_status(job)
            if undo_status is not None and undo_status.total_messages > 0:
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
        package = await self._repository.get_archive_package_for_job(job.id)
        if package is not None:
            deletion = await self._safe_archive_deletion_status(package)
            if deletion is not None:
                if deletion.complete:
                    text += (
                        "\n\n🗑 **远端归档** · 已按记录删除全部文件；"
                        "Telegram 与删除审计仍保留。"
                    )
                elif deletion.commit_boundary_invalidated:
                    text += (
                        f"\n\n🧹 **远端归档清理** · 已删 `{deletion.deleted_targets}/"
                        f"{deletion.total_targets}`，剩余 `{deletion.remaining_count}` 个精确文件。"
                    )
                else:
                    text += (
                        f"\n\n⚠️ **远端归档删除待确认** · 精确目标 "
                        f"`{deletion.remaining_count}` 个，尚未开始删除内容。"
                    )
        control = await self._repository.get_job_control(job.id)
        if control.hold_requested and not job.terminal:
            text += "\n\n⏸ **任务已暂停** · 当前缓存已保留，恢复后从安全边界继续。"
        return text

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
