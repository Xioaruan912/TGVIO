from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import shutil
import time
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
from tgvio.domain.maintenance import business_day_bounds
from tgvio.domain.operations import UndoStatus
from tgvio.domain.publish import PublishPlan, PublishStepKind, PublishStepState, PublishTarget
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event


from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.bot_ui_format import BotUIFormatMixin




from tgvio.adapters.telegram.bot_ui_job_actions import BotUIJobActionsMixin


class BotUIJobsMixin(BotUIJobActionsMixin):
    async def _display_number(self, job: Job) -> int | None:
        get_display = getattr(self._repository, "get_display_no", None)
        if callable(get_display):
            value = await get_display(job.id)
            if value is not None:
                return int(value)
        get_order = getattr(self._repository, "get_accepted_order", None)
        accepted_order = await get_order(job.id) if callable(get_order) else None
        return None if accepted_order is None else int(accepted_order)

    async def _job_label(self, job: Job) -> str:
        return self._job_number(await self._display_number(job))

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
            kwargs: dict[str, object] = {
                "owner_id": owner_id,
                "filter": filter,
                "page": page,
                "page_size": page_size,
            }
            if filter == JobListFilter.TODAY:
                bounds = business_day_bounds(time.time(), hour=6)
                kwargs["business_day_start_epoch"] = bounds.start_epoch
            return await page_jobs(**kwargs)

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
            if filter == JobListFilter.RUNNING and job.terminal:
                continue
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
            if filter == JobListFilter.TODAY:
                bounds = business_day_bounds(time.time(), hour=6)
                created = job.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created.timestamp() < bounds.start_epoch:
                    continue
            if filter == JobListFilter.PENDING and job.terminal and job.state != JobState.FAILED:
                continue
            if filter == JobListFilter.HISTORY:
                continue
            accepted_order = await self._display_number(job)
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
        filter: JobListFilter | None = None,
        page: int = 0,
    ) -> tuple[str, list]:
        if filter is None:
            # Open where the user's attention is: running work, then problems.
            running = await self._load_jobs_page(owner_id, filter=JobListFilter.RUNNING, page=0)
            if not running.total:
                if (await self._load_failure_page(owner_id, page=0)).total:
                    return await self._failures_page(owner_id)
            filter = JobListFilter.RUNNING if running.total else JobListFilter.COMPLETED
        result = await self._load_jobs_page(owner_id, filter=filter, page=page)
        label = JOB_FILTER_LABELS[result.filter]
        lines = [f"📋 **我的任务 · {label}**（{result.total}）", ""]
        if not result.entries:
            lines.append(
                "现在没有进行中的任务。转发视频给我就会开始。"
                if result.filter == JobListFilter.RUNNING
                else "这里还没有任务。"
            )
        for entry in result.entries:
            lines.extend(await self._job_list_lines(entry.job, entry.label_number, held=entry.held))
        rows: list[list] = []
        if result.entries:
            rows.append(
                [
                    Button.inline(
                        f"{self._job_state_icon(entry.job.state, held=entry.held)} "
                        + (f"#{entry.label_number}" if entry.label_number is not None else "查看"),
                        self._callback_data("job", entry.job.id),
                    )
                    for entry in result.entries
                ]
            )
            lines.extend(["", "点上面的编号看详情。"])
        rows.extend(
            self._page_nav(
                result.page,
                result.total_pages,
                lambda number: f"ui:jobs:{result.filter.value}:{number}".encode(),
            )
        )
        rows.append(self._task_tabs(f"ui:jobs:{result.filter.value}:0".encode()))
        rows.append([Button.inline("🗂 更早的", b"ui:jobs:history:0"), Button.inline("🏠 首页", b"ui:home")])
        return "\n".join(lines), rows

    async def _job_list_lines(self, job: Job, number: int | None, *, held: bool = False) -> list[str]:
        icon = self._job_state_icon(job.state, held=held)
        state = "已暂停" if held else STATE_LABELS[job.state]
        lines = [
            f"{icon} **#{number if number is not None else '?'}** · {state} · "
            f"{self._job_media_counts(job)} · {self._job_local_time(job)}",
            f"　{self._job_content_summary(job)}",
        ]
        if job.state == JobState.FAILED:
            lines.append(f"　⚠️ {describe_job_failure(job.error_code).title}")
        elif not job.terminal and not held:
            progress = await self._repository.get_job_progress(job.id)
            if progress is not None:
                lines.append(f"　{self._progress_text(progress)}")
        return lines

    @staticmethod
    def _task_tabs(current: bytes) -> list:
        return [
            Button.inline(("• " if data == current else "") + label, data)
            for label, data in TASK_TABS
        ]

    @staticmethod
    def _page_nav(page: int, total_pages: int, data_for: Callable[[int], bytes]) -> list[list]:
        if total_pages <= 1:
            return []
        nav = []
        if page > 0:
            nav.append(Button.inline("⬅️ 上一页", data_for(page - 1)))
        nav.append(Button.inline(f"{page + 1}/{total_pages}", data_for(page)))
        if page + 1 < total_pages:
            nav.append(Button.inline("下一页 ➡️", data_for(page + 1)))
        return [nav]

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
                    display_no=await self._display_number(job),
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
        plan = await self._retry_all_plan(owner_id)
        lines = [f"⚠️ **有问题的任务**（{result.total}）", ""]
        if not result.entries:
            lines.append("✅ 没有需要你处理的问题。出错的任务会先自动重试。")
        for entry in result.entries:
            job = entry.job
            lines.append(
                f"❌ **#{entry.label_number if entry.label_number is not None else '?'}** · "
                f"{self._job_media_counts(job)} · {self._job_local_time(job)}"
            )
            lines.append(f"　{self._job_content_summary(job)}")
            if entry.job_actionable:
                issue = describe_job_failure(job.error_code)
                lines.append(f"　{issue.title}：{issue.action}")
            if entry.archive_actionable:
                issue = describe_archive_failure(entry.archive_error_code)
                lines.append(f"　☁️ {issue.title}：{issue.action}")
        if plan is not None and plan.skipped_items:
            lines.extend(["", f"📎 另有 {plan.skipped_items} 个文件之前没下载到，可以补发。"])
        if plan is not None:
            lines.extend(["", "点“🔁 全部重试”一次处理所有能重试的；已经发出去的不会重复发。"])
        rows: list[list] = self._retry_all_button(plan)
        if result.entries:
            rows.append(
                [
                    Button.inline(
                        f"#{entry.label_number}" if entry.label_number is not None else "查看",
                        self._callback_data("job", entry.job.id),
                    )
                    for entry in result.entries
                ]
            )
        rows.extend(
            self._page_nav(result.page, result.total_pages, lambda number: f"ui:failures:{number}".encode())
        )
        rows.append(self._task_tabs(b"ui:failures:0"))
        rows.append([Button.inline("🏠 首页", b"ui:home")])
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
        text = self._render_job_diagnostic(
            snapshot,
            deep=deep,
            accepted_order=await self._display_number(job),
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
        return self._render_plan(job, plan, accepted_order=await self._display_number(job))

    async def _resolve_job(self, owner_id: int, prefix: str | None) -> Job | None:
        normalized = prefix.strip().lower() if prefix else ""
        numeric = normalized.removeprefix("#")
        if numeric.isdigit():
            get_by_display = getattr(self._repository, "get_by_display_number", None)
            if callable(get_by_display):
                day = business_day_bounds(time.time()).day
                exact = await get_by_display(int(owner_id), day, int(numeric))
                if exact is not None:
                    return exact
            # A short numeric value is exclusively a human display number for
            # the current business day; old global numbers no longer resolve.
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
