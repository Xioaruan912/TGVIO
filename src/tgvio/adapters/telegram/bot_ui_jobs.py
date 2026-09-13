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




from tgvio.adapters.telegram.bot_ui_job_actions import BotUIJobActionsMixin


class BotUIJobsMixin(BotUIJobActionsMixin):
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
