"""Startup recovery planner for durable R3 jobs.

No Telegram or WebDAV IO is performed here. The planner only inspects durable
state/local cache and applies repository transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any


@dataclass(frozen=True)
class RecoveryAction:
    job: Any
    action: str
    paths: tuple[str, ...] = ()
    reason: str | None = None


async def recover_jobs(repository: Any) -> list[RecoveryAction]:
    actions: list[RecoveryAction] = []
    for original in await repository.list_incomplete_jobs():
        job = original
        items = await repository.list_job_items(job.id)
        paths = tuple(str(item.local_path) for item in items if item.local_path)
        cache_ok = bool(paths) and len(paths) == len(items) and all(
            os.path.isfile(path) and os.path.getsize(path) == item.size_bytes
            for item, path in zip(items, paths)
        )

        async def move(to_state: str, event_type: str, reason: str):
            nonlocal job
            result = await repository.transition_job(
                job.id,
                expected_revision=job.revision,
                to_state=to_state,
                event_type=event_type,
                payload={"schema_version": 1, "reason": reason},
            )
            job = result.job
            return result.applied

        if job.state == "queued":
            if job.source_kind == "url" and job.source_url:
                actions.append(RecoveryAction(job, "download"))
            else:
                await move("failed", "recovery_failed", "source_descriptor_unavailable")
                actions.append(RecoveryAction(job, "failed", reason="source_descriptor_unavailable"))
            continue

        if job.state == "downloading":
            await move("interrupted", "recovery_interrupted", "process_restarted_during_download")
            if job.source_kind == "url" and job.source_url:
                await move("queued", "recovery_requeued", "url_download_can_restart")
                actions.append(RecoveryAction(job, "download"))
            else:
                await move("failed", "recovery_failed", "source_descriptor_unavailable")
                actions.append(RecoveryAction(job, "failed", reason="source_descriptor_unavailable"))
            continue

        if job.state == "ready":
            if cache_ok:
                actions.append(RecoveryAction(job, "publish", paths))
            else:
                await move("failed", "recovery_failed", "local_cache_missing_or_incomplete")
                actions.append(RecoveryAction(job, "failed", reason="local_cache_missing_or_incomplete"))
            continue

        if job.state == "publishing":
            refs = await repository.list_published_messages(job.id)
            await move("interrupted", "recovery_interrupted", "process_restarted_during_publish")
            if refs:
                await move("failed", "recovery_failed", "published_refs_exist_manual_review")
                actions.append(RecoveryAction(job, "failed", reason="published_refs_exist_manual_review"))
            elif cache_ok:
                await move("ready", "recovery_ready", "no_published_refs_safe_to_retry")
                actions.append(RecoveryAction(job, "publish", paths))
            else:
                await move("failed", "recovery_failed", "local_cache_missing_or_incomplete")
                actions.append(RecoveryAction(job, "failed", reason="local_cache_missing_or_incomplete"))
            continue

        if job.state == "interrupted":
            if cache_ok:
                await move("ready", "recovery_ready", "complete_local_cache")
                actions.append(RecoveryAction(job, "publish", paths))
            elif job.source_kind == "url" and job.source_url:
                await move("queued", "recovery_requeued", "url_download_can_restart")
                actions.append(RecoveryAction(job, "download"))
            else:
                await move("failed", "recovery_failed", "no_safe_resume_path")
                actions.append(RecoveryAction(job, "failed", reason="no_safe_resume_path"))
    return actions
