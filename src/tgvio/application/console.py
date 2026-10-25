"""What the web console can see and do, as JSON-ready values.

The HTTP adapter (adapters/web) only authenticates, parses and serialises; every
read and every action goes through here, reusing the services the bot already
owns (job control, bulk retry, archive retry, cache cleanup, publish styles).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import shutil
from pathlib import Path
from typing import Any

from tgvio.application.auto_recovery import (
    archive_failure_waits_for_recovery,
    job_failure_waits_for_recovery,
    job_recovery_state,
)
from tgvio.application.bulk_retry import NOT_RETRYABLE_CODES, BulkRetryService
from tgvio.application.job_control import JobControlService, UnsafeRetryError
from tgvio.application.ports import ArchiveOperator, CacheOperator, JobRepository
from tgvio.application.publish_styles import (
    BUILTIN_STYLES,
    DEFAULT_LABEL,
    DEFAULT_STYLE,
    STYLE_ORDER,
    PublishStyleService,
    resolve_style,
    style_policy,
)
from tgvio.application.result_card import ResultCardService
from tgvio.application.user_messages import describe_archive_failure, describe_job_failure
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.intake import SpoilerMode
from tgvio.domain.job import Job, JobState, job_failure_code
from tgvio.domain.job_query import JobListFilter

STATE_LABELS = {
    JobState.RECEIVED: "排队中",
    JobState.DOWNLOADING: "下载中",
    JobState.DOWNLOADED: "下载完成",
    JobState.ANALYZING: "检查中",
    JobState.ANALYZED: "检查完成",
    JobState.PLANNED: "等待发布",
    JobState.PUBLISHING: "发布中",
    JobState.SUCCEEDED: "已完成",
    JobState.FAILED: "失败",
    JobState.CANCELLED: "已取消",
}
ARCHIVE_LABELS = {
    ArchivePackageState.PLANNED: "排队中",
    ArchivePackageState.STAGING: "准备中",
    ArchivePackageState.UPLOADING: "上传中",
    ArchivePackageState.VERIFYING: "核对中",
    ArchivePackageState.COMMITTED: "已备份",
    ArchivePackageState.FAILED: "备份失败",
    ArchivePackageState.CANCELLED: "已取消",
}
# Spoiler choices offered on the web; "ask" needed a chat round-trip and is gone.
SPOILER_MODES = (SpoilerMode.SOURCE, SpoilerMode.ALWAYS_SPOILER, SpoilerMode.ALWAYS_NORMAL)
SETTING_FLAGS = ("alerts_enabled", "daily_cleanup_enabled")
JOB_FILTERS = (JobListFilter.RUNNING, JobListFilter.FAILED, JobListFilter.COMPLETED, JobListFilter.HISTORY)
_RESUMABLE = (
    JobState.RECEIVED,
    JobState.DOWNLOADING,
    JobState.DOWNLOADED,
    JobState.ANALYZING,
    JobState.ANALYZED,
    JobState.PLANNED,
    JobState.PUBLISHING,
)


class ConsoleError(RuntimeError):
    """An action the console refuses, with a message the page can show."""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class ConsoleBaseline:
    destination: str
    cover_mode: bool
    forward_caption: bool
    archive_enabled: bool
    archive_root: str
    download_dir: Path


class ConsoleService:
    def __init__(
        self,
        repository: JobRepository,
        *,
        owner_id: int,
        baseline: ConsoleBaseline,
        control: JobControlService,
        schedule: Callable[[Job], None],
        bulk_retry: BulkRetryService,
        runtime_flags: Any,
        archive: ArchiveOperator | None = None,
        cache: CacheOperator | None = None,
    ) -> None:
        self._repository = repository
        self._owner_id = int(owner_id)
        self._baseline = baseline
        self._control = control
        self._schedule = schedule
        self._bulk_retry = bulk_retry
        self._flags = runtime_flags
        self._archive = archive
        self._cache = cache
        self._results = ResultCardService(repository)
        self._styles = PublishStyleService(
            repository,
            style_policy(cover_mode=baseline.cover_mode, forward_caption=baseline.forward_caption),
        )

    @property
    def owner_id(self) -> int:
        return self._owner_id

    # ------------------------------------------------------------------ jobs
    async def jobs(self, filter: str, page: int) -> dict[str, Any]:
        try:
            job_filter = JobListFilter(filter)
        except ValueError:
            raise ConsoleError("unknown filter", status=400) from None
        if job_filter == JobListFilter.FAILED:
            failures = await self._repository.page_failures(
                owner_id=self._owner_id, page=max(0, int(page)), page_size=20
            )
            entries = [(entry.job, entry.label_number, False) for entry in failures.entries]
            total, pages, current = failures.total, failures.total_pages, failures.page
        else:
            result = await self._repository.page_jobs(
                owner_id=self._owner_id, filter=job_filter, page=max(0, int(page)), page_size=20
            )
            entries = [(entry.job, entry.label_number, entry.held) for entry in result.entries]
            total, pages, current = result.total, result.total_pages, result.page
        return {
            "filter": job_filter.value,
            "page": current,
            "pages": pages,
            "total": total,
            "items": [await self._job_summary(job, number, held) for job, number, held in entries],
        }

    async def job(self, job_id: str) -> dict[str, Any]:
        job = await self._owned(job_id)
        summary = await self._job_summary(job, await self._repository.get_display_no(job.id), None)
        card = await self._results.build(job)
        summary.update(
            {
                "items": [
                    {
                        "index": item.index,
                        "kind": item.kind.value,
                        "name": item.name,
                        "size_bytes": item.size_bytes,
                        "duration_seconds": item.duration_seconds,
                        "width": item.width,
                        "height": item.height,
                    }
                    for item in job.items
                ],
                "confirmed_messages": card.confirmed_messages,
                "link_url": card.link_url,
                "skipped_items": card.skipped_items,
                "skipped_reason": card.skipped_reason,
            }
        )
        return summary

    async def retry(self, job_id: str) -> dict[str, Any]:
        job = await self._owned(job_id)
        try:
            decision = await self._control.retry_failed(job)
        except UnsafeRetryError:
            raise ConsoleError("频道里可能已经发出了这个任务的内容，为防重复没有重试，请去频道看一下。") from None
        except ValueError:
            raise ConsoleError("任务状态已变化，请刷新后再试。") from None
        self._schedule(decision.job)
        return await self.job(job_id)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = await self._owned(job_id)
        try:
            await self._control.request_cancel(job, reason=f"requested by owner {self._owner_id} (web)")
        except ValueError:
            raise ConsoleError("任务已经结束或状态已变化，无法取消。") from None
        return await self.job(job_id)

    async def hold(self, job_id: str) -> dict[str, Any]:
        job = await self._owned(job_id)
        try:
            await self._control.request_hold(job, reason=f"requested by owner {self._owner_id} (web)")
        except ValueError:
            raise ConsoleError("任务已经结束或状态已变化，无法暂停。") from None
        return await self.job(job_id)

    async def resume(self, job_id: str) -> dict[str, Any]:
        job = await self._owned(job_id)
        try:
            await self._control.resume(job)
        except ValueError:
            raise ConsoleError("任务已经结束或状态已变化，无法恢复。") from None
        self._schedule(job)
        return await self.job(job_id)

    async def retry_archive(self, job_id: str) -> dict[str, Any]:
        job = await self._owned(job_id)
        package = await self._repository.get_archive_package_for_job(job.id)
        if package is None or self._archive is None:
            raise ConsoleError("这个任务没有网盘备份。")
        try:
            await self._archive.retry_package(package.id)
        except (KeyError, ValueError):
            raise ConsoleError("备份状态已变化，请刷新后再试。") from None
        return await self.job(job_id)

    async def retry_all(self) -> dict[str, Any]:
        plan = await self._bulk_retry.plan(self._owner_id)
        result = await self._bulk_retry.run(self._owner_id, plan)
        for decision in result.retried:
            self._schedule(decision.job)
        for child in result.recovery_children:
            self._schedule(child)
        return {
            "retried": len(result.retried),
            "archives": result.archives_requeued,
            "recovered": len(result.recovery_children),
            "unchanged": result.unchanged,
        }

    # ---------------------------------------------------------------- status
    async def status(self) -> dict[str, Any]:
        queue = await self._repository.get_queue_control()
        counts = await self._repository.count_by_state(owner_id=self._owner_id)
        archive_counts = await self._repository.count_archive_packages_by_state(owner_id=self._owner_id)
        cache = await self._cache.stats() if self._cache is not None else None
        free = _disk_free(self._baseline.download_dir)
        return {
            "queue_paused": bool(queue.paused),
            "jobs": {state.value: int(counts.get(state, 0)) for state in JobState},
            "archive": {
                "enabled": self._baseline.archive_enabled,
                "root": self._baseline.archive_root,
                "states": {state.value: int(archive_counts.get(state, 0)) for state in ArchivePackageState},
            },
            "cache": None if cache is None else {
                "bytes_used": cache.bytes_used,
                "eligible_jobs": cache.eligible_jobs,
                "blocked_by_archive": cache.blocked_by_archive,
                "retention_hours": cache.retention_hours,
            },
            "disk_free_bytes": free,
            "destination": self._baseline.destination,
        }

    async def pause_queue(self) -> dict[str, Any]:
        await self._control.pause_queue(reason=f"requested by owner {self._owner_id} (web)")
        return await self.status()

    async def resume_queue(self) -> dict[str, Any]:
        await self._control.resume_queue()
        for job in await self._repository.list_by_states(_RESUMABLE):
            if not (await self._repository.get_job_control(job.id)).hold_requested:
                self._schedule(job)
        return await self.status()

    async def clean_cache(self) -> dict[str, Any]:
        if self._cache is None:
            raise ConsoleError("缓存清理未启用。")
        result = await self._cache.cleanup(force=True)
        return {"removed_jobs": result.removed_jobs, "removed_bytes": result.removed_bytes,
                "blocked_by_archive": result.blocked_by_archive}

    # -------------------------------------------------------------- settings
    async def settings(self) -> dict[str, Any]:
        preference = await self._repository.get_user_preference(self._owner_id)
        name, policy = resolve_style(preference.style_json, self._styles_baseline())
        spoiler = preference.spoiler_mode
        if spoiler not in SPOILER_MODES:
            spoiler = SpoilerMode.SOURCE
        return {
            "style": name,
            "style_policy": policy,
            "styles": [{"key": DEFAULT_STYLE, "label": DEFAULT_LABEL, **self._styles_baseline()}] + [
                {"key": key, "label": BUILTIN_STYLES[key]["label"],
                 "cover_mode": BUILTIN_STYLES[key]["cover_mode"],
                 "forward_caption": BUILTIN_STYLES[key]["forward_caption"]}
                for key in STYLE_ORDER
            ],
            "spoiler_mode": spoiler.value,
            "spoiler_modes": [mode.value for mode in SPOILER_MODES],
            "flags": {key: self._flags.bool(key, True) for key in SETTING_FLAGS},
        }

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "style" in payload:
            style = payload["style"]
            if style == DEFAULT_STYLE:
                await self._styles.reset(self._owner_id)
            elif isinstance(style, str) and style in BUILTIN_STYLES:
                await self._styles.set_named(self._owner_id, style)
            else:
                raise ConsoleError("unknown style", status=400)
        if "spoiler_mode" in payload:
            try:
                mode = SpoilerMode(str(payload["spoiler_mode"]))
            except ValueError:
                raise ConsoleError("unknown spoiler mode", status=400) from None
            if mode not in SPOILER_MODES:
                raise ConsoleError("unknown spoiler mode", status=400)
            await self._repository.set_user_spoiler_mode(self._owner_id, mode)
        flags = payload.get("flags")
        if flags is not None:
            if not isinstance(flags, dict) or any(key not in SETTING_FLAGS or not isinstance(value, bool)
                                                   for key, value in flags.items()):
                raise ConsoleError("invalid flags", status=400)
            for key, value in flags.items():
                await self._flags.set(self._repository, key, "true" if value else "false")
        return await self.settings()

    # --------------------------------------------------------------- helpers
    def _styles_baseline(self) -> dict[str, object]:
        return style_policy(cover_mode=self._baseline.cover_mode, forward_caption=self._baseline.forward_caption)

    async def _owned(self, job_id: str) -> Job:
        job = await self._repository.get(str(job_id))
        if job is None or int(job.owner_id) != self._owner_id:
            raise ConsoleError("任务不存在", status=404)
        return job

    async def _job_summary(self, job: Job, number: int | None, held: bool | None) -> dict[str, Any]:
        control = await self._repository.get_job_control(job.id)
        if held is None:
            held = control.hold_requested and not job.terminal
        package = await self._repository.get_archive_package_for_job(job.id)
        progress = None if job.terminal else await self._repository.get_job_progress(job.id)
        code = job_failure_code(job) if job.state == JobState.FAILED else None
        issue = describe_job_failure(code) if code is not None else None
        return {
            "id": job.id,
            "number": number,
            "state": job.state.value,
            "state_label": "已暂停" if held else STATE_LABELS[job.state],
            "held": bool(held),
            "cancel_requested": bool(control.cancel_requested) and not job.terminal,
            "archive_only": bool(job.policy.get("archive_only")),
            "created_at": job.created_at,
            "media": {kind: sum(1 for item in job.items if item.kind.value == kind)
                      for kind in ("video", "photo", "document")},
            "size_bytes": sum(int(item.size_bytes or 0) for item in job.items),
            "title": _job_title(job),
            "error": None if issue is None else {
                "code": code, "title": issue.title, "explanation": issue.explanation,
            },
            "retryable": (
                job.state == JobState.FAILED
                and code not in NOT_RETRYABLE_CODES
                and not job_failure_waits_for_recovery(job)
                and job_recovery_state(job).get("status") != "manual_review"
            ),
            "progress": None if progress is None else {
                "phase": progress.phase, "current": progress.current, "total": progress.total,
                "item_index": progress.item_index, "item_total": progress.item_total,
            },
            "archive": _archive_summary(job, package),
        }


def _job_title(job: Job) -> str:
    for item in job.items:
        caption = str(item.caption or "").strip()
        if caption:
            return caption.splitlines()[0][:80]
    for item in job.items:
        if item.name:
            return str(item.name)[:80]
    return ""


def _archive_summary(job: Job, package: ArchivePackage | None) -> dict[str, Any] | None:
    if package is None:
        return None
    issue = describe_archive_failure(package.error_code) if package.state == ArchivePackageState.FAILED else None
    stored = sum(1 for obj in package.objects if obj.state.value == "stored")
    return {
        "state": package.state.value,
        "state_label": ARCHIVE_LABELS.get(package.state, package.state.value),
        "path": package.remote_path,
        "stored": stored,
        "objects": len(package.objects),
        "retryable": package.state == ArchivePackageState.FAILED and not archive_failure_waits_for_recovery(job, package),
        "error": None if issue is None else {"title": issue.title, "explanation": issue.explanation},
    }


def _disk_free(path: Path) -> int | None:
    try:
        return int(shutil.disk_usage(path if path.exists() else path.parent).free)
    except OSError:
        return None
