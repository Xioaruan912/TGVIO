from __future__ import annotations

from dataclasses import dataclass

from tgvio.application.ports import JobRepository, OperationalLogReader
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.job import Job, JobEvent, JobState
from tgvio.domain.progress import JobProgress
from tgvio.domain.publish import PublishEffect, PublishPlan, PublishStepState


@dataclass(frozen=True, slots=True)
class DiagnosticHint:
    severity: str
    code: str
    summary: str
    action: str | None = None


@dataclass(frozen=True, slots=True)
class JobDiagnosticSnapshot:
    job: Job
    events: tuple[JobEvent, ...]
    progress: JobProgress | None
    plan: PublishPlan | None
    effects: tuple[PublishEffect, ...]
    archive: ArchivePackage | None
    archive_event_count: int
    recent_logs: tuple[dict[str, object], ...]
    hints: tuple[DiagnosticHint, ...]


class JobDiagnosticService:
    """Correlate durable state with structured logs without changing job state."""

    def __init__(
        self,
        repository: JobRepository,
        logs: OperationalLogReader | None = None,
    ) -> None:
        self._repository = repository
        self._logs = logs

    async def inspect(self, job: Job, *, log_limit: int = 40) -> JobDiagnosticSnapshot:
        events = tuple(await self._repository.list_events(job.id))
        progress = await self._repository.get_job_progress(job.id)
        plan = await self._repository.get_publish_plan(job.id)
        effects: tuple[PublishEffect, ...] = ()
        if plan is not None:
            effects = tuple(await self._repository.list_publish_effects(plan.id))
        archive = await self._repository.get_archive_package_for_job(job.id)
        archive_event_count = 0
        if archive is not None:
            archive_event_count = len(await self._repository.list_archive_events(archive.id))
        recent_logs: tuple[dict[str, object], ...] = ()
        if self._logs is not None:
            recent_logs = tuple(
                await self._logs.recent_for_job(
                    job.id,
                    plan_id=(plan.id if plan else None),
                    package_id=(archive.id if archive else None),
                    limit=log_limit,
                )
            )
        hints = self._build_hints(job, plan, effects, archive, recent_logs)
        return JobDiagnosticSnapshot(
            job=job,
            events=events,
            progress=progress,
            plan=plan,
            effects=effects,
            archive=archive,
            archive_event_count=archive_event_count,
            recent_logs=recent_logs,
            hints=hints,
        )

    @classmethod
    def _build_hints(
        cls,
        job: Job,
        plan: PublishPlan | None,
        effects: tuple[PublishEffect, ...],
        archive: ArchivePackage | None,
        logs: tuple[dict[str, object], ...],
    ) -> tuple[DiagnosticHint, ...]:
        hints: list[DiagnosticHint] = []
        code = job.error_code or ""

        if code == "publish_uncertain":
            hints.append(
                DiagnosticHint(
                    "critical",
                    code,
                    "Telegram 可见发送的结果不确定；系统已阻止盲目重发。",
                    "先核对目标频道中的实际消息，再决定是否人工恢复。",
                )
            )
        elif code == "publish_partial":
            hints.append(
                DiagnosticHint(
                    "critical",
                    code,
                    "发布步骤已确认部分频道消息，但没有完整提交结果。",
                    "不要直接重试；先检查目标频道中已经出现的消息。",
                )
            )
        elif code == "disk_low":
            hints.append(
                DiagnosticHint(
                    "error",
                    code,
                    "下载在开始传输前被磁盘保留空间保护阻止。",
                    "释放 managed cache 或增加可用磁盘后再安全重试。",
                )
            )
        elif code == "download_failed":
            hints.append(
                DiagnosticHint(
                    "error",
                    code,
                    "任务失败于下载阶段，尚未产生 Telegram 发布副作用。",
                    "网络或来源恢复后，可在任务详情中点“重试任务”。",
                )
            )
        elif code == "media_analysis_failed":
            hints.append(
                DiagnosticHint(
                    "error",
                    code,
                    "媒体分析失败，发布尚未开始。",
                    "可以先安全重试；仍失败时请换一个文件或重新导出媒体。",
                )
            )
        elif code == "publish_failed" and any(
            str(row.get("exception_type", "")) == "BotMethodInvalidError"
            for row in logs
        ):
            hints.append(
                DiagnosticHint(
                    "error",
                    "telegram_bot_method_invalid",
                    "Telegram 拒绝了仅用户账号可调用的方法。",
                    "需要管理员检查 Telegram 适配逻辑；这不是媒体文件故障。",
                )
            )
        elif code == "publish_failed":
            hints.append(
                DiagnosticHint(
                    "error",
                    code,
                    "发布失败；能否重试取决于是否已有确认的频道消息。",
                    "点“重试任务”后系统会检查记录，只有确认安全时才会继续。",
                )
            )

        if plan is not None:
            failed_steps = [step for step in plan.steps if step.state == PublishStepState.FAILED]
            for step in failed_steps[:2]:
                confirmed = cls._confirmed_effect_count(effects, step.index)
                hints.append(
                    DiagnosticHint(
                        "warning",
                        step.error_code or "publish_step_failed",
                        f"发布步骤 {step.index + 1} 失败；当前有 {confirmed} 条已确认消息记录。",
                        "确认记录数量和实际频道消息一致后再处理这个步骤。",
                    )
                )

        if archive is not None and archive.state == ArchivePackageState.FAILED:
            failed_objects = sum(1 for obj in archive.objects if obj.state.value == "failed")
            hints.append(
                DiagnosticHint(
                    "warning",
                    archive.error_code or "archive_failed",
                    f"Telegram 发布与 WebDAV 归档相互独立；当前有 {failed_objects} 个文件归档失败。",
                    "本地缓存会继续受保护，可在任务详情中点“重传失败归档”。",
                )
            )
        elif (
            job.state == JobState.SUCCEEDED
            and archive is not None
            and archive.state != ArchivePackageState.COMMITTED
        ):
            hints.append(
                DiagnosticHint(
                    "warning",
                    "archive_pending_after_publish",
                    "Telegram 已发布成功，但 WebDAV 归档尚未完成。",
                    "不要清理该任务缓存；等待归档完成或打开“归档”页面检查。",
                )
            )

        recent_error_events = [
            str(row.get("event"))
            for row in logs
            if str(row.get("level", "")).upper() in {"ERROR", "CRITICAL"}
        ]
        if recent_error_events and not hints:
            hints.append(
                DiagnosticHint(
                    "warning",
                    "operational_log_error",
                    f"结构化日志中检测到错误事件：{recent_error_events[-1]}。",
                    "在任务详情中点“技术详情”查看最近日志时间线。",
                )
            )

        if not hints and job.state == JobState.SUCCEEDED:
            if archive is None or archive.state == ArchivePackageState.COMMITTED:
                hints.append(
                    DiagnosticHint(
                        "info",
                        "healthy_terminal",
                        "未发现需要人工处理的持久化异常。",
                    )
                )

        return tuple(hints[:5])

    @staticmethod
    def _confirmed_effect_count(effects: tuple[PublishEffect, ...], step_index: int) -> int:
        return sum(
            1
            for effect in effects
            if effect.step_index == step_index
            and effect.effect_type != "publish_step_receipts_committed"
        )
