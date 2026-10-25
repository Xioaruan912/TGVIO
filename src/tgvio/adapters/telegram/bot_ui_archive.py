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
from tgvio.config import Settings
from tgvio.domain.archive import (
    ArchivePackage,
    ArchivePackageState,
)
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


from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.bot_ui_format import BotUIFormatMixin




class BotUIArchiveMixin:
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
        payload = self._archive_retry_operation_payload(
            job,
            package,
            event_count=len(archive_events),
        )
        confirmation_ref = await self._issue_job_operation(
            job,
            owner_id=owner_id,
            action="archive_retry",
            expected_revision=len(archive_events),
            payload=payload,
        )
        label = await self._job_label(job)
        stored = sum(1 for obj in package.objects if obj.state.value == "stored")
        failed_objects = sum(1 for obj in package.objects if obj.state.value == "failed")
        await self._edit_page(
            event,
            (
                f"**确认重传归档 · {label}**\n\n"
                f"Profile：`{package.archive_profile_id}` · 策略：**{self._archive_policy_label(package.archive_policy.value)}**\n"
                f"已确认：`{stored}/{len(package.objects)}` · 失败对象：`{failed_objects}`\n\n"
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
        payload = self._archive_retry_operation_payload(
            job,
            package,
            event_count=len(archive_events),
        )
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

    async def _archive_text(self, owner_id: int) -> str:
        enabled = bool(getattr(self._settings, "archive_enabled", False))
        profile_id = str(getattr(self._settings, "archive_profile_id", "primary"))
        policy = str(getattr(self._settings, "archive_policy", "required"))
        policy_label = self._archive_policy_label(policy)
        capability = await get_archive_capability_status(
            self._repository,
            profile_id=profile_id,
        )
        lines = [
            "**WebDAV 归档**",
            "",
            f"状态：`{'开启' if enabled else '关闭'}`",
            f"Profile：`{profile_id}` · 策略：**{policy_label}**",
            f"远端根目录：`{getattr(self._settings, 'archive_remote_root', 'TGVIO')}`",
            self._archive_capability_summary(capability),
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
                failed_objects = sum(1 for obj in package.objects if obj.state.value == "failed")
                remaining = max(0, len(package.objects) - stored - failed_objects)
                total_bytes = sum(obj.size_bytes for obj in package.objects)
                job = await self._repository.get(package.job_id)
                label = await self._job_label(job) if job is not None else "任务"
                package_policy = self._archive_policy_label(package.archive_policy.value)
                lines.append(
                    f"• {label} · {ARCHIVE_STATE_LABELS[package.state]} · {package_policy} · "
                    f"`{stored}/{len(package.objects)}` 已存 · "
                    f"待处理 `{remaining}` · 失败 `{failed_objects}` · {self._human_bytes(total_bytes)}"
                )
                if package.state == ArchivePackageState.FAILED:
                    issue = describe_archive_failure(package.error_code)
                    if job is not None and archive_failure_waits_for_recovery(job, package):
                        recovery = archive_recovery_state(job)
                        if recovery.get("status") == "scheduled":
                            retry_at = self._epoch_local_text(recovery.get("next_retry_epoch"))
                            suffix = f" · 计划 {retry_at}" if retry_at else ""
                            lines.append(
                                f"  ↳ 系统将自动续传第 {recovery.get('next_attempt', '?')}/"
                                f"{recovery.get('max_attempts', '?')} 次{suffix}，无需操作。"
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
