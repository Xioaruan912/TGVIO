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




class BotUIFixtureMixin:
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

