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


class BotUIFormatMixin:
    async def _context_home(self, owner_id: int, chat_id: int):
        """Read owner-scoped local state; navigation never schedules work."""
        lines = ["👋 **TGVIO**", ""]
        buttons = []
        try:
            stats = await self._repository.get_stats_snapshot(owner_id=owner_id)
            counts = await self._repository.count_by_state(owner_id=owner_id)
            active = sum(count for state, count in counts.items() if state not in {
                JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
            })
            failures = await self._repository.page_failures(
                owner_id=owner_id, page=0, page_size=1,
            )
            retry_plan = await self._retry_all_plan(owner_id)
            lines.append(
                f"今天已发布 **{int(stats.get('today_succeeded', 0) or 0)}** 个 · "
                f"进行中 **{active}** 个"
            )
            attention = failures.total + (
                len(retry_plan.skipped_parent_ids) if retry_plan is not None else 0
            )
            if attention:
                lines.extend(["", f"⚠️ 有 **{attention}** 项需要你看一下。"])
                buttons.extend(self._retry_all_button(retry_plan))
                buttons.append([Button.inline("⚠️ 看看是什么问题", b"ui:failures:0")])
            if active:
                buttons.append([Button.inline("⏳ 看进度", b"ui:jobs:running:0")])
            session = await self._repository.get_open_collection(owner_id, chat_id)
            if session is not None:
                media, texts = await self._repository.count_collection_entries(session.id)
                lines.extend([
                    "",
                    f"📥 合集收集中：{media} 项媒体、{texts} 段文字。",
                    "继续转发就会加进来，好了点“预览发布”。",
                ])
                buttons.append([Button.inline(
                    "👀 预览发布", f"intake:preview:{session.id}".encode(),
                )])
            drafts = await self._repository.list_drafts(owner_id, limit=20)
            saved = [draft for draft in drafts if not draft.active]
            if saved:
                lines.append(f"📝 有 {len(saved)} 份草稿没发。")
                buttons.append([Button.inline("📝 继续编辑草稿", b"ui:drafts:0")])
        except Exception:
            lines.append("暂时无法读取完整状态，请稍后刷新；已有任务不受影响。")
        lines.extend([
            "",
            "**怎么用：** 直接把视频、图片转发给我，我会自动发到频道。",
            "想把几条消息合成一组发？点下方“📥 新建合集”。",
        ])
        if not getattr(self._settings, "publish_enabled", False):
            lines.append("⏸ 自动发布已关闭，现在不会发到频道。")
        buttons.append([
            Button.inline("📋 我的任务", b"ui:jobs"),
            Button.inline("🔄 刷新", b"ui:home"),
        ])
        return "\n".join(lines), buttons

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
            "❓ **怎么用**\n\n"
            "**发视频**\n"
            "直接把视频、图片或文件转发给我。我会下载好再发到频道，完成后通知你。\n\n"
            "**几条消息合成一组发**\n"
            "1. 点下方“📥 新建合集”\n"
            "2. 把要发的内容都转发给我\n"
            "3. 点“👀 预览与整理”，确认后发布\n\n"
            "**出问题了怎么办**\n"
            "大部分问题会自动重试，不用管。需要你处理时首页会提示，点“🔁 全部重试”就行。\n\n"
            "**下方按钮**\n"
            "📋 我的任务：看进度、看发过的内容\n"
            "⚙️ 设置：发布风格、草稿、收藏、系统状态"
        )
        if self._settings.url_enabled:
            text += "\n\n🔗 也可以直接发一个视频网页链接给我。"
        if getattr(self._settings, "live_fixture_enabled", False):
            text += (
                "\n\n🧪 受控发布已启用：`/publish #任务序号`，例如 `/publish #24`。"
                "该命令需要二次确认，且只允许小型安全 fixture。"
            )
        return text

    @staticmethod
    def _help_buttons():
        return [[Button.inline("📋 我的任务", b"ui:jobs"), Button.inline("🏠 首页", b"ui:home")]]

    async def _diag_text(self) -> str:
        if self._diagnostic_service is None:
            return (
                "**TGVIO Diagnostic Snapshot**\n\n"
                "诊断快照服务未启用。\n\n"
                "本页不会主动连接 Telegram、WebDAV 或代理，也不会执行写探测。"
            )
        snapshot = await self._diagnostic_service.snapshot()
        return self._render_diagnostic_snapshot(snapshot)

    @staticmethod
    def _render_diagnostic_snapshot(snapshot: DiagnosticSnapshot) -> str:
        proxy_checked = (
            "未检测"
            if snapshot.static_proxy.checked_at_epoch is None
            else datetime.fromtimestamp(
                snapshot.static_proxy.checked_at_epoch,
                tz=timezone.utc,
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
        )
        schema_version = (
            "unknown"
            if snapshot.schema.user_version is None
            else str(snapshot.schema.user_version)
        )
        latest_version = (
            "unknown"
            if snapshot.schema.latest_version is None
            else str(snapshot.schema.latest_version)
        )
        generation = (
            "unknown"
            if snapshot.runtime_lease.generation is None
            else str(snapshot.runtime_lease.generation)
        )
        yes_no = lambda value: "yes" if value else "no"
        on_off = lambda value: "on" if value else "off"
        lines = [
            "**TGVIO Diagnostic Snapshot**",
            "",
            "**Release identity**",
            f"Release：`{snapshot.release_id}`",
            f"Version：`{snapshot.app_version}`",
            f"Commit：`{snapshot.commit}`",
            f"Source manifest：`{snapshot.source_manifest}`",
            "",
            "**Schema / runtime**",
            (
                f"Schema：`v{schema_version}` / latest `v{latest_version}` · "
                f"ledger contiguous `{yes_no(snapshot.schema.ledger_contiguous)}` · "
                f"verification `{snapshot.schema.verification.value}`"
            ),
            (
                f"Runtime lease：unique `{yes_no(snapshot.runtime_lease.unique)}` · "
                f"generation `{generation}` · `{snapshot.runtime_lease.freshness.value}`"
            ),
            f"Aggregate query：`{snapshot.aggregate_status.value}`",
            "",
            "**Scheduler**",
            f"Queue paused：`{yes_no(snapshot.scheduler.paused)}`",
            (
                f"Active `{snapshot.scheduler.active}` · Held `{snapshot.scheduler.held}` · "
                f"Ready `{snapshot.scheduler.ready}` · Blocked `{snapshot.scheduler.blocked}`"
            ),
            "",
            "**Archive**",
            (
                f"Planned `{snapshot.archive.planned}` · Transferring `{snapshot.archive.transferring}` · "
                f"Committed `{snapshot.archive.committed}` · Failed `{snapshot.archive.failed}` · "
                f"Retry-wait `{snapshot.archive.retry_wait}`"
            ),
            f"Capability freshness：`{snapshot.archive.capability_freshness}`",
            "",
            "**Feature flags**",
            (
                f"Bot `{on_off(snapshot.features.run_bot)}` · Publish `{on_off(snapshot.features.publish_enabled)}` · "
                f"URL `{on_off(snapshot.features.url_enabled)}`/`{snapshot.features.url_private_network_policy}`"
            ),
            (
                f"Archive `{on_off(snapshot.features.archive_enabled)}`/`{snapshot.features.archive_policy}` · "
                f"Collections `{on_off(snapshot.features.collections_enabled)}` · "
                f"Auto-retry `{on_off(snapshot.features.auto_retry_enabled)}` · "
                f"Fixture `{on_off(snapshot.features.live_fixture_enabled)}`"
            ),
            "",
            "**Static proxy**",
            f"State：`{snapshot.static_proxy.state.value}` · Last check：`{proxy_checked}`",
            "",
            *(
                [
                    "**Capabilities**",
                    (
                        f"ffmpeg `{on_off(snapshot.capabilities.ffmpeg)}` · "
                        f"ffprobe `{on_off(snapshot.capabilities.ffprobe)}` · "
                        f"yt-dlp `{on_off(snapshot.capabilities.yt_dlp)}` · "
                        f"cryptg `{on_off(snapshot.capabilities.cryptg)}` · "
                        f"hachoir `{on_off(snapshot.capabilities.hachoir)}`"
                    ),
                    "",
                ]
                if snapshot.capabilities is not None
                else []
            ),
            "只读本地 SQLite 与脱敏启动状态；不会主动连接 Telegram、WebDAV、代理或其它外部服务。",
        ]
        return "\n".join(lines)

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
                f"{self._phase_status(event_types, 'job_created', None)} 收到",
                f"{self._phase_status(event_types, 'download_completed', 'download_failed', 'download_started')} 下载",
                f"{self._phase_status(event_types, 'analysis_completed', 'analysis_failed', 'analysis_started')} 检查文件",
                f"{'✅' if snapshot.plan is not None else '▫️'} 准备发布",
                f"{self._publish_status(snapshot)} 发到频道",
                f"{self._archive_status(snapshot)} 云端备份",
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
                    "**发到频道**",
                    f"已发出：`{succeeded}/{len(snapshot.plan.steps)}` 组"
                    + (f" · 消息记录 `{len(external_effects)}`" if deep else ""),
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
                    "**云端备份**",
                    f"{ARCHIVE_STATE_LABELS[archive.state]} · 文件 `{stored}/{len(archive.objects)}` · `{self._human_bytes(sum(obj.size_bytes for obj in archive.objects))}`",
                ]
            )
            if deep:
                lines.append(
                    f"Profile：`{archive.archive_profile_id}` · 策略：**{self._archive_policy_label(archive.archive_policy.value)}** · policy v`{archive.archive_policy_version}`"
                )
            if archive.state == ArchivePackageState.FAILED:
                issue = describe_archive_failure(archive.error_code)
                recovery = archive_recovery_state(job)
                status = str(recovery.get("status", ""))
                if status == "scheduled":
                    retry_at = self._epoch_local_text(recovery.get("next_retry_epoch"))
                    retry_text = f" · 计划 `{retry_at}`" if retry_at else ""
                    lines.extend(
                        [
                            issue.explanation,
                            f"会自动接着上传（第 {recovery.get('next_attempt', '?')}/{recovery.get('max_attempts', '?')} 次{retry_text}），不用管。",
                        ]
                    )
                elif status in {"exhausted", "abandoned"}:
                    lines.extend(
                        [
                            issue.explanation,
                            "自动续传停了；频道里的内容不受影响，可以点“☁️ 重新备份”。",
                        ]
                    )
                elif archive_failure_waits_for_recovery(job, archive):
                    lines.extend([issue.explanation, "正在自动处理，不用管。"])
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

    @staticmethod
    def _archive_policy_label(policy: str) -> str:
        return "尽力归档" if policy == "best_effort" else "必须归档"

    @staticmethod
    def _archive_retry_operation_payload(
        job: Job,
        package: ArchivePackage,
        *,
        event_count: int,
    ) -> dict[str, object]:
        objects = sorted(package.objects, key=lambda value: value.object_index)
        return {
            "job_id": job.id,
            "package_id": package.id,
            "state": package.state.value,
            "error_code": package.error_code or "",
            "event_count": int(event_count),
            "archive_profile_id": package.archive_profile_id,
            "archive_policy": package.archive_policy.value,
            "archive_policy_version": package.archive_policy_version,
            "object_states": [
                {
                    "index": obj.object_index,
                    "state": obj.state.value,
                    "retry_count": obj.retry_count,
                }
                for obj in objects
            ],
            "failed_object_indexes": [
                obj.object_index for obj in objects if obj.state.value == "failed"
            ],
        }

    @classmethod
    def _archive_capability_summary(cls, status: ArchiveCapabilityStatus) -> str:
        if status.freshness == "unknown":
            base = "能力：`未确认`"
        else:
            freshness = "已确认" if status.freshness == "fresh" else "已过期"
            commit_mode = status.commit_mode or "unknown"
            confirmed = cls._epoch_local_text(status.confirmed_at_epoch)
            time_text = f" · {confirmed}" if confirmed else ""
            base = f"能力：`{freshness}` · commit `{commit_mode}`{time_text}"
        if status.last_probe_status == "unreachable":
            return base + " · 最近检测：`失败（保留上次确认能力）`"
        if status.last_probe_status == "reachable":
            return base + " · 最近检测：`可达`"
        return base + " · 最近检测：`未知`"

    @staticmethod
    def _epoch_local_text(value: object) -> str | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%m-%d %H:%M")

    def _progress_text(self, progress: JobProgress) -> str:
        phase_labels = {
            "queued": "排队",
            "downloading": "下载",
            "downloaded": "下载完成",
            "analyzing": "检查",
            "analyzed": "检查完成",
            "planned": "等待发布",
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
            [text_button(COLLECTION_NEW_BUTTON), text_button(NAV_JOBS)],
            [text_button(NAV_SETTINGS), text_button(NAV_HELP)],
        ]

    @staticmethod
    def _more_text() -> str:
        return (
            "**更多工具**\n\n"
            "这里是统计、运行健康和脱敏技术诊断。日常转发一般不需要打开这些页面。"
        )

    def _more_buttons(self):
        return [
            [Button.inline("☁️ 归档", b"ui:archive"), Button.inline("🧹 缓存", b"ui:cache")],
            [Button.inline("📊 状态", b"ui:status"), Button.inline("🗂 历史", b"ui:jobs:history:0")],
            [Button.inline("⭐ 收藏夹", b"ui:favorites:0"), Button.inline("📈 统计", b"ui:stats")],
            [Button.inline("❤️ 运行健康", b"ui:health"), Button.inline("🩺 技术诊断", b"ui:diag")],
            [Button.inline("❓ 使用帮助", b"ui:help"), Button.inline("⚙️ 设置", b"ui:settings")],
            [Button.inline("📋 我的任务", b"ui:jobs"), Button.inline("🏠 首页", b"ui:home")],
        ]

    def _setting_on(self, key: str, default: bool = True) -> bool:
        flags = getattr(self, "_runtime_flags", None)
        if flags is None:
            return default
        try:
            return flags.bool(key, default)
        except Exception:
            return default

    def _settings_page_text(self, quiet: bool = False) -> str:
        def label(value: bool) -> str:
            return "✅ 开" if value else "⛔ 关"

        flags = getattr(self, "_runtime_flags", None)
        cleanup_time = flags.get("daily_cleanup_time", "06:00") if flags else "06:00"
        return (
            "⚙️ **设置**\n\n"
            f"🔔 出错时提醒我：{label(self._setting_on('alerts_enabled'))}\n"
            f"📦 合集发布前先预览：{label(self._setting_on('collection_preview_enabled'))}\n"
            f"🧹 每天 {cleanup_time} 自动整理：{label(self._setting_on('daily_cleanup_enabled'))}\n"
            "　　（收起已完成的任务、清理缓存，记录都会保留）\n"
            f"🔕 安静模式：{label(quiet)}\n"
            "　　（开启后少发中间进度，结果和出错提醒照常）\n\n"
            "点下面的按钮切换，或进入其他设置。"
        )

    def _settings_page_buttons(self, quiet: bool = False):
        def toggle_label(value: bool) -> str:
            return "✅" if value else "⛔"

        return [
            [
                Button.inline(f"🔔 出错提醒 {toggle_label(self._setting_on('alerts_enabled'))}", b"set:alerts_enabled"),
                Button.inline(f"📦 先预览 {toggle_label(self._setting_on('collection_preview_enabled'))}", b"set:collection_preview_enabled"),
            ],
            [
                Button.inline(f"🧹 自动整理 {toggle_label(self._setting_on('daily_cleanup_enabled'))}", b"set:daily_cleanup_enabled"),
                Button.inline(f"🔕 安静 {toggle_label(quiet)}", b"set:quiet_mode"),
            ],
            [Button.inline("🎨 发布风格", b"ui:styles"), Button.inline("📝 草稿", b"ui:drafts:0")],
            [Button.inline("⭐ 收藏", b"ui:favorites:0"), Button.inline("🗂 发过的内容", b"ui:jobs:history:0")],
            [Button.inline("🧩 内容与下载", b"ui:content"), Button.inline("🔐 来源账号", b"ui:source")],
            [Button.inline("🔧 系统状态", b"ui:status")],
            [Button.inline("🏠 首页", b"ui:home")],
        ]

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

    def _plan_buttons(self, job: Job):
        return [
            [Button.inline("🔎 任务详情", self._callback_data("job", job.id))],
            [Button.inline("← 我的任务", b"ui:jobs"), Button.inline("🏠 首页", b"ui:home")],
        ]
