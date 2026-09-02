"""R1 queue facade isolating Telegram handlers from pipeline internals."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import os
import time
from types import SimpleNamespace
from typing import Any

from ..models import Job, PendingJob, RetryInfo, Session


@dataclass(frozen=True)
class RetryTicket:
    old_seq: int
    new_seq: int
    info: RetryInfo
    cached_path: str
    cleanup_extra: str


@dataclass(frozen=True)
class ConfirmationTicket:
    seq: int
    pending: PendingJob


class JobQueue:
    """Handler-facing queue facade over the existing in-memory pipeline.

    R1 deliberately preserves worker/storage behavior. All handler mutations of
    queue dictionaries are routed through this object, which gives R2/R3 a
    stable seam for repository-backed transitions and idempotency.
    """

    def __init__(self, pipeline: Any, shadow: Any | None = None) -> None:
        self._pipeline = pipeline
        self._shadow = shadow

    @property
    def pipeline(self) -> Any:
        return self._pipeline

    def task_label(self, seq: int) -> str:
        return self._pipeline.task_label(seq)

    def view_state(self, user_id: int):
        return self._pipeline._queue_view_state(user_id)

    def spoiler_mode(self, user_id: int) -> str:
        return self._pipeline._spoiler_mode(user_id)

    def set_spoiler_mode(self, user_id: int, mode: str) -> None:
        self._pipeline.set_spoiler_mode(user_id, mode)

    def toggle_progress(self, user_id: int) -> bool:
        return self._pipeline.toggle_progress_pref(user_id)

    def progress_enabled(self, user_id: int) -> bool:
        return self._pipeline._show_progress(user_id)

    def _owns_runtime_job(self, seq: int, user_id: int) -> bool:
        seq = int(seq)
        owner = getattr(self._pipeline, "_seq_owners", {}).get(seq)
        if owner is None:
            pending = getattr(self._pipeline, "pending", {}).get(seq)
            job = (
                getattr(self._pipeline, "jobs", {}).get(seq)
                or getattr(self._pipeline, "_runtime_jobs", {}).get(seq)
            )
            retry = getattr(self._pipeline, "retryable", {}).get(seq)
            candidate = pending or job or getattr(retry, "job", None)
            owner = getattr(candidate, "user_id", None)
        try:
            return int(owner) == int(user_id)
        except (TypeError, ValueError):
            return False

    async def home_snapshot(self, user_id: int) -> dict[str, Any]:
        repository = getattr(self._pipeline, "repository", None)
        if repository is not None:
            counts = await repository.count_jobs_by_state(user_id=user_id)
            running = sum(counts.get(state, 0) for state in ("downloading", "publishing"))
            waiting = sum(
                counts.get(state, 0)
                for state in ("collecting", "awaiting_confirmation", "queued", "ready", "interrupted", "paused")
            )
            failed = counts.get("failed", 0)
        else:
            running = len(self._pipeline._download_tasks) + (1 if self._pipeline._uploading else 0)
            waiting = max(0, len(self._pipeline.active_seqs) - running)
            failed = len(self._pipeline.retryable)
        session = self.session(user_id)
        disk_used = disk_total = None
        disk_reserved = disk_protected = disk_reclaimable = 0.0
        disk_enforce = False
        try:
            manager = getattr(self._pipeline, "disk", None)
            if manager is not None:
                usage = manager.snapshot()
                disk_used = usage.used / (1024 ** 3)
                disk_total = usage.total / (1024 ** 3)
                disk_reserved = usage.reserved / (1024 ** 3)
                disk_enforce = bool(manager.enforce)
                cleanup = await self.disk_cleanup_plan()
                if cleanup is not None:
                    disk_protected = cleanup.protected_bytes / (1024 ** 3)
                    disk_reclaimable = cleanup.reclaimable_bytes / (1024 ** 3)
            else:
                import shutil

                usage = shutil.disk_usage(self._pipeline.download_dir)
                disk_used = (usage.total - usage.free) / (1024 ** 3)
                disk_total = usage.total / (1024 ** 3)
        except OSError:
            pass
        return {
            "session_active": session is not None,
            "session_media": session.media_count if session is not None else 0,
            "session_texts": session.text_count if session is not None else 0,
            "running": running,
            "waiting": waiting,
            "failed": failed,
            "paused": bool(self._pipeline._paused),
            "disk_used_gb": disk_used,
            "disk_total_gb": disk_total,
            "disk_reserved_gb": disk_reserved,
            "disk_protected_gb": disk_protected,
            "disk_reclaimable_gb": disk_reclaimable,
            "disk_enforce": disk_enforce,
        }

    async def disk_cleanup_plan(self):
        """Build an F3 cleanup dry run; this method never deletes files."""
        repository = getattr(self._pipeline, "repository", None)
        manager = getattr(self._pipeline, "disk", None)
        if repository is None or manager is None:
            return None
        inventory = await repository.cleanup_inventory()
        retry_ids: set[int] = set()
        webdav_ids: set[int] = set()
        for seq in getattr(self._pipeline, "retryable", {}):
            job_id = self.durable_job_id(int(seq))
            if job_id is not None:
                retry_ids.add(int(job_id))
        for seq in getattr(self._pipeline, "webdav_keep_cache", set()):
            job_id = self.durable_job_id(int(seq))
            if job_id is not None:
                webdav_ids.add(int(job_id))
        return manager.cleanup_plan(
            inventory,
            cache_retention_hours=getattr(self._pipeline, "_disk_cache_retention_hours", 72.0),
            failed_retention_hours=getattr(self._pipeline, "_disk_failed_retention_hours", 168.0),
            retry_protected_job_ids=retry_ids,
            webdav_protected_job_ids=webdav_ids,
        )

    async def cleanup_to_waterline(self, *, force: bool = False) -> dict[str, int]:
        """Execute validated cleanup candidates until capacity/quota is healthy."""
        repository = getattr(self._pipeline, "repository", None)
        manager = getattr(self._pipeline, "disk", None)
        if repository is None or manager is None:
            return {"cleaned": 0, "failed": 0, "freed_bytes": 0}
        plan = await self.disk_cleanup_plan()
        if plan is None:
            return {"cleaned": 0, "failed": 0, "freed_bytes": 0}
        total_cache = plan.reclaimable_bytes + plan.protected_bytes
        owner = f"disk-{os.getpid()}"
        cleaned = failed = freed = 0

        def needs_cleanup() -> bool:
            quota_low = bool(manager.max_cache_bytes and total_cache > manager.max_cache_bytes)
            return force or not manager.healthy() or quota_low

        for candidate in plan.candidates:
            if not needs_cleanup():
                break
            claim = await repository.acquire_disk_cleanup_claim(
                candidate.job_id,
                expected_revision=candidate.revision,
                owner=owner,
            )
            if claim is None:
                continue
            result = manager.cleanup_candidate(candidate)
            freed += int(result.freed_bytes)
            total_cache = max(0, total_cache - int(result.freed_bytes))
            if result.complete:
                committed = await asyncio.shield(
                    repository.finish_disk_cleanup(
                        candidate.job_id,
                        expected_revision=claim.revision,
                        owner=owner,
                        freed_bytes=result.freed_bytes,
                    )
                )
                if committed:
                    cleaned += 1
                    continue
            await asyncio.shield(
                repository.abort_disk_cleanup_claim(
                    candidate.job_id,
                    expected_revision=claim.revision,
                    owner=owner,
                    freed_bytes=result.freed_bytes,
                )
            )
            failed += 1
        return {"cleaned": cleaned, "failed": failed, "freed_bytes": freed}

    async def durable_queue_page(
        self,
        user_id: int,
        *,
        filter_name: str = "all",
        page: int = 0,
        page_size: int = 5,
    ) -> dict[str, Any] | None:
        repository = getattr(self._pipeline, "repository", None)
        if repository is None:
            return None
        completed_since = time.time() - 24 * 3600
        rows, total = await repository.page_jobs(
            user_id=user_id,
            filter_name=filter_name,
            page=page,
            page_size=page_size,
            completed_since=completed_since,
        )
        counts = await repository.count_jobs_by_state(user_id=user_id)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(0, int(page)), pages - 1)
        if total and page * page_size >= total:
            rows, total = await repository.page_jobs(
                user_id=user_id,
                filter_name=filter_name,
                page=page,
                page_size=page_size,
                completed_since=completed_since,
            )
        return {
            "filter_name": filter_name,
            "page": page,
            "pages": pages,
            "total": total,
            "running": sum(counts.get(state, 0) for state in ("downloading", "publishing")),
            "waiting": sum(
                counts.get(state, 0)
                for state in ("collecting", "awaiting_confirmation", "queued", "ready", "interrupted")
            ),
            "paused": counts.get("paused", 0),
            "failed": counts.get("failed", 0),
            "items": rows,
        }

    async def durable_job_detail(self, user_id: int, job_id: int) -> dict[str, Any] | None:
        repository = getattr(self._pipeline, "repository", None)
        if repository is None:
            return None
        detail = await repository.job_detail(job_id, user_id=user_id)
        if detail is None:
            return None
        cache_exists = False
        compat_labels: list[str] = []
        for item in detail.pop("items", []):
            path = item.get("local_path")
            size = int(item.get("size_bytes") or 0)
            if path and os.path.isfile(path):
                try:
                    if size <= 0 or os.path.getsize(path) == size:
                        cache_exists = True
                except OSError:
                    pass
            raw_metadata = item.get("metadata_json")
            if raw_metadata:
                try:
                    metadata = json.loads(raw_metadata)
                except Exception:
                    metadata = {}
                compat = metadata.get("media_compat") if isinstance(metadata, dict) else None
                if isinstance(compat, dict):
                    video = compat.get("video_codec")
                    audio = compat.get("audio_codec")
                    if compat.get("streaming_ready"):
                        compat_labels.append("✅ 可流式播放")
                    elif video not in (None, "h264") or audio not in (None, "aac"):
                        compat_labels.append("⚠️ 非 H.264/AAC")
                    elif compat.get("faststart") is False:
                        compat_labels.append("🧩 建议 faststart")
        detail["cache_exists"] = cache_exists
        detail["media_compat_summary"] = " · ".join(dict.fromkeys(compat_labels))[:300]
        seq = detail.get("legacy_seq")
        detail["can_retry"] = bool(seq is not None and int(seq) in self._pipeline.retryable)
        backup = detail.get("backup")
        if backup:
            remote_dir = str(backup.get("remote_dir") or "")
            detail["backup_summary"] = os.path.basename(remote_dir.rstrip("/")) or "最近一次尝试"
        else:
            detail["backup_summary"] = ""
        detail["error_message"] = str(detail.get("error_message") or "")[:1000]
        snapshot_raw = detail.pop("destination_profile_snapshot_json", None)
        detail["destination_profile"] = ""
        if snapshot_raw:
            try:
                snapshot = json.loads(snapshot_raw)
                detail["destination_profile"] = str(snapshot.get("name") or "")[:80]
            except Exception:
                detail["destination_profile"] = ""
        return detail

    async def durable_record(self, user_id: int, job_id: int):
        repository = getattr(self._pipeline, "repository", None)
        if repository is None:
            return None
        record = await repository.get_job(job_id)
        if record is None or record.user_id != int(user_id):
            return None
        return record

    def durable_job_id(self, seq: int) -> int | None:
        if self._shadow is None:
            return None
        value = self._shadow.job_ids.get(int(seq))
        return int(value) if value is not None else None

    async def cancel_job_by_id(self, user_id: int, job_id: int, expected_revision: int) -> str:
        record = await self.durable_record(user_id, job_id)
        if record is None:
            return "missing"
        if record.revision != int(expected_revision):
            return "stale"
        if record.state in {"succeeded", "cancelled", "failed"}:
            return "terminal"
        repository = getattr(self._pipeline, "repository", None)
        if record.legacy_seq is not None and await self.cancel(
            int(record.legacy_seq), user_id=user_id
        ):
            return "ok"
        if repository is None:
            return "unavailable"
        current = await repository.get_job(job_id)
        if current is None or current.revision != int(expected_revision):
            return "stale"
        result = await repository.transition_job(
            job_id,
            expected_revision=expected_revision,
            to_state="cancelled",
            event_type="cancelled_ui",
            payload={"schema_version": 1, "source": "u2", "runtime_missing": True},
        )
        return "ok" if result.applied else "stale"

    async def retry_job_by_id(self, user_id: int, job_id: int, expected_revision: int):
        record = await self.durable_record(user_id, job_id)
        if record is None:
            return "missing", None
        if record.revision != int(expected_revision):
            return "stale", None
        if record.state != "failed" or record.legacy_seq is None:
            return "terminal", None
        ticket = self.claim_retry(int(record.legacy_seq), user_id=user_id)
        return ("ok", ticket) if ticket is not None else ("unavailable", None)

    async def delete_failed_cache_by_id(self, user_id: int, job_id: int, expected_revision: int) -> str:
        repository = getattr(self._pipeline, "repository", None)
        if repository is None:
            return "unavailable"
        detail = await repository.job_detail(job_id, user_id=user_id)
        if detail is None:
            return "missing"
        if int(detail["revision"]) != int(expected_revision):
            return "stale"
        if str(detail["state"]) != "failed":
            return "terminal"
        paths: list[str] = []
        root = os.path.realpath(self._pipeline.download_dir)
        for item in detail.get("items", []):
            raw = item.get("local_path")
            if not raw:
                continue
            path = os.path.realpath(str(raw))
            try:
                if os.path.commonpath([root, path]) != root:
                    continue
            except ValueError:
                continue
            paths.append(path)
        if not await repository.clear_failed_cache(job_id, expected_revision=expected_revision):
            return "stale"
        for path in paths:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        return "ok"

    async def delete_history_by_id(
        self,
        user_id: int,
        job_id: int,
        expected_revision: int,
    ) -> str:
        repository = getattr(self._pipeline, "repository", None)
        if repository is None:
            return "unavailable"
        return await repository.delete_job_history(
            int(job_id),
            user_id=int(user_id),
            expected_revision=int(expected_revision),
        )

    async def published_refs_by_id(self, user_id: int, job_id: int, expected_revision: int):
        repository = getattr(self._pipeline, "repository", None)
        record = await self.durable_record(user_id, job_id)
        if repository is None or record is None:
            return "missing", []
        if record.revision != int(expected_revision):
            return "stale", []
        refs = [item for item in await repository.list_published_messages(job_id) if item.deleted_at is None]
        return "ok", refs

    async def mark_published_deleted_by_id(
        self,
        user_id: int,
        job_id: int,
        expected_revision: int,
        record_ids: list[int],
    ) -> bool:
        repository = getattr(self._pipeline, "repository", None)
        record = await self.durable_record(user_id, job_id)
        if repository is None or record is None or record.revision != int(expected_revision):
            return False
        return await repository.mark_published_deleted(
            job_id,
            expected_revision=expected_revision,
            message_ids=record_ids,
        )

    async def batch_targets(self, user_id: int, kind: str) -> list[dict[str, Any]]:
        repository = getattr(self._pipeline, "repository", None)
        if repository is None:
            return []
        return await repository.batch_targets(user_id=user_id, kind=kind)

    def session(self, user_id: int) -> Session | None:
        return self._pipeline.sessions.get(user_id)

    def has_session(self, user_id: int) -> bool:
        return user_id in self._pipeline.sessions

    def begin_session(self, user_id: int) -> Session:
        session = self._pipeline.sessions.get(user_id)
        if session is None:
            session = Session(user_id=user_id)
            self._pipeline.sessions[user_id] = session
        return session

    async def finalize_session(self, user_id: int, chat_id: int) -> int:
        return await self._pipeline._session_finalize(user_id, chat_id)

    async def add_session_batch(self, user_id: int, messages: list, chat_id: int) -> None:
        await self._pipeline._session_add_batch(user_id, messages, chat_id)

    async def add_session_text(self, user_id: int, text: str, chat_id: int) -> None:
        await self._pipeline._session_add_text(user_id, text, chat_id)

    def collect_album(self, grouped_id: int, message: object, chat_id: int) -> None:
        self._pipeline.collect_album(grouped_id, message, chat_id)

    async def auto_enqueue(
        self,
        kind: str,
        message: object,
        album: list | None,
        user_id: int,
        *,
        force_normal: bool = False,
        texts: list | None = None,
        reserved_seq: int | None = None,
        spoiler_override: bool | None = None,
        destination_profile_id: int | None = None,
        destination_profile_snapshot: dict[str, Any] | None = None,
        allow_album_merge: bool = True,
    ) -> int:
        return await self._pipeline._auto_enqueue(
            kind,
            message,
            album,
            user_id,
            force_normal=force_normal,
            texts=texts,
            reserved_seq=reserved_seq,
            spoiler_override=spoiler_override,
            destination_profile_id=destination_profile_id,
            destination_profile_snapshot=destination_profile_snapshot,
            allow_album_merge=allow_album_merge,
        )

    def reserve_seq(self) -> int:
        return self._pipeline.reserve_seq()

    async def show_confirmation(
        self,
        seq: int,
        kind: str,
        message: object,
        album: list | None,
        user_id: int,
        chat_id: int,
        texts: list | None = None,
    ) -> None:
        await self._pipeline._show_ask(
            seq,
            kind,
            message,
            album,
            user_id,
            chat_id,
            texts=texts,
        )

    def submit_url(
        self,
        status: object,
        message: object,
        url: str,
        user_id: int,
    ) -> int:
        return self._pipeline.submit(
            "url",
            status,
            message,
            url,
            user_id=user_id,
        )

    async def cancel_pending(self, seq: int, *, user_id: int) -> bool:
        if not self._owns_runtime_job(seq, user_id):
            return False
        ok = await self._pipeline._cancel_pending(seq)
        if ok and self._shadow is not None:
            if getattr(self._pipeline, "repository", None) is not None:
                await self._shadow.transition_now(seq, "cancelled", "cancelled")
            else:
                self._shadow.transition(seq, "cancelled", "cancelled")
        return ok

    async def cancel(self, seq: int, *, user_id: int) -> bool:
        if not self._owns_runtime_job(seq, user_id):
            return False
        ok = await self._pipeline._cancel_seq(seq)
        if ok and self._shadow is not None:
            if getattr(self._pipeline, "repository", None) is not None:
                await self._shadow.transition_now(seq, "cancelled", "cancelled")
            else:
                self._shadow.transition(seq, "cancelled", "cancelled")
            if (
                getattr(self._pipeline, "repository", None) is not None
                and seq not in self._pipeline._download_tasks
                and seq not in self._pipeline._upload_tasks
            ):
                self._pipeline._finish_seq(seq)
        return ok

    def pause_all(self) -> None:
        self._pipeline._paused = True

    def resume_all(self) -> None:
        self._pipeline._paused = False

    def stop_running(self, seq: int, *, user_id: int) -> bool:
        if not self._owns_runtime_job(seq, user_id):
            return False
        task = self._pipeline._download_tasks.get(seq)
        if task is None or task.done():
            task = self._pipeline._upload_tasks.get(seq)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def pop_published(self, seq: int, *, user_id: int):
        if not self._owns_runtime_job(seq, user_id):
            return None
        return self._pipeline.published.pop(seq, None)

    def hold(self, seq: int, *, user_id: int) -> Job | None:
        if not self._owns_runtime_job(seq, user_id):
            return None
        self._pipeline._paused_files.add(seq)
        job = self._pipeline.jobs.get(seq)
        if job is not None and self._shadow is not None:
            self._shadow.transition(seq, "paused", "paused")
        return job

    def resume(self, seq: int, *, user_id: int) -> Job | None:
        if not self._owns_runtime_job(seq, user_id):
            return None
        self._pipeline._paused_files.discard(seq)
        job = self._pipeline.jobs.get(seq)
        if job is not None and self._shadow is not None:
            self._shadow.resume(seq)
        return job

    def claim_retry(self, seq: int, *, user_id: int) -> RetryTicket | None:
        if not self._owns_runtime_job(seq, user_id):
            return None
        info = self._pipeline.retryable.pop(seq, None)
        if info is None:
            return None
        new_seq = self._pipeline.reserve_seq()
        cached = info.path or ""
        cleanup = os.path.dirname(cached) if cached else ""
        return RetryTicket(
            old_seq=seq,
            new_seq=new_seq,
            info=info,
            cached_path=cached,
            cleanup_extra=cleanup,
        )

    async def retry_failed_durable(
        self,
        seq: int,
        *,
        user_id: int,
        status: object,
    ) -> tuple[str, Job | None]:
        """Retry a failed durable job after in-memory retry state was lost.

        This is intentionally conservative: every cached item must still be
        present with the recorded size, and there must be no persisted publish
        side effects. The original durable job is moved back to ``ready`` and
        reattached to the runtime queue without re-downloading Telegram media.
        """
        repository = getattr(self._pipeline, "repository", None)
        if repository is None:
            return "unavailable", None

        record = None
        for candidate in await repository.list_jobs(user_id=int(user_id), limit=500):
            if candidate.legacy_seq is not None and int(candidate.legacy_seq) == int(seq):
                record = candidate
                break
        if record is None:
            return "missing", None
        if record.state != "failed":
            return "terminal", None

        refs = await repository.list_published_messages(record.id)
        if refs:
            return "partial", None

        items = await repository.list_job_items(record.id)
        if not items:
            return "cache_missing", None
        paths: list[str] = []
        placeholders: list[object] = []
        for item in items:
            path = str(item.local_path or "")
            if not path or not os.path.isfile(path):
                return "cache_missing", None
            try:
                if int(item.size_bytes or 0) > 0 and os.path.getsize(path) != int(item.size_bytes):
                    return "cache_missing", None
            except OSError:
                return "cache_missing", None
            paths.append(path)
            caption = ""
            if item.metadata_json:
                try:
                    caption = str(json.loads(item.metadata_json).get("caption") or "")
                except Exception:
                    caption = ""
            placeholders.append(SimpleNamespace(message=caption))

        transition = await repository.transition_job(
            record.id,
            expected_revision=record.revision,
            to_state="ready",
            event_type="retry_requested",
            payload={"schema_version": 1, "source": "durable_retry", "legacy_seq": int(seq)},
        )
        if not transition.applied:
            return "stale", None
        record = transition.job

        texts = await repository.list_job_texts(record.id)
        snapshot = None
        if record.destination_profile_snapshot_json:
            try:
                snapshot = json.loads(record.destination_profile_snapshot_json)
            except Exception:
                snapshot = None
        job = Job(
            seq=int(seq),
            kind=record.kind,
            status=status,
            message=(placeholders[0] if placeholders else SimpleNamespace(message="")),
            album=(placeholders if record.kind in {"album", "collection"} else None),
            url=record.source_url or "",
            spoiler=record.spoiler,
            user_id=record.user_id,
            texts=texts or None,
            destination_profile_id=record.destination_profile_id,
            destination_profile_snapshot=snapshot,
        )
        if self._shadow is not None:
            self.bind_recovered(int(seq), record.id)
        self._pipeline._runtime_jobs[int(seq)] = job
        self._pipeline.jobs[int(seq)] = job
        self._pipeline.active_seqs.add(int(seq))
        self._pipeline._remember_seq_owner(int(seq), int(record.user_id))
        payload = paths if record.kind in {"album", "collection"} else (paths[0] if len(paths) == 1 else paths)
        self._pipeline._set_result(int(seq), payload)
        self._pipeline._counter = max(self._pipeline._counter, int(seq) + 1)
        return "ok", job

    def commit_retry(self, ticket: RetryTicket, status: object) -> Job:
        old_job = ticket.info.job
        new_job = Job(
            seq=ticket.new_seq,
            kind=old_job.kind,
            status=status,
            message=old_job.message,
            album=old_job.album,
            url=old_job.url,
            spoiler=old_job.spoiler,
            user_id=old_job.user_id,
            cached_path=ticket.cached_path,
            cleanup_extra=ticket.cleanup_extra,
            texts=old_job.texts,
            destination_profile_id=getattr(old_job, "destination_profile_id", None),
            destination_profile_snapshot=getattr(old_job, "destination_profile_snapshot", None),
        )
        self._pipeline.active_seqs.add(ticket.new_seq)
        self._pipeline.enqueue(new_job)
        if self._shadow is not None:
            self._shadow.event(ticket.old_seq, "retry_requested", retry_seq=ticket.new_seq)
            self._shadow.accept(
                ticket.new_seq,
                kind=new_job.kind,
                user_id=new_job.user_id,
                state="queued",
                source_kind="url" if new_job.url else "telegram",
                message=new_job.message,
                album=new_job.album,
                texts=list(new_job.texts or []),
                source_url=new_job.url or None,
                spoiler=new_job.spoiler,
                destination_profile_id=new_job.destination_profile_id,
                destination_profile_snapshot=new_job.destination_profile_snapshot,
                event_type="retry_accepted",
                event_extra={"retry_of_legacy_seq": ticket.old_seq},
            )
        return new_job

    def claim_confirmation(self, seq: int, *, user_id: int) -> ConfirmationTicket | None:
        if not self._owns_runtime_job(seq, user_id):
            return None
        pending = self._pipeline.pending.pop(seq, None)
        if pending is None:
            return None
        if pending.timeout_task is not None:
            pending.timeout_task.cancel()
        self._pipeline.active_seqs.add(seq)
        return ConfirmationTicket(seq=seq, pending=pending)

    async def select_pending_destination(self, seq: int, profile_id: int, *, user_id: int) -> str:
        pending = self._pipeline.pending.get(int(seq))
        profiles = getattr(self._pipeline, "destination_profiles", None)
        if pending is None or int(pending.user_id or 0) != int(user_id):
            return "missing"
        if profiles is None:
            return "unavailable"
        profile = await profiles.repository.get_destination_profile(int(profile_id))
        if profile is None or not profile.enabled:
            return "profile_unavailable"
        snapshot = profiles.repository.destination_profile_snapshot(profile)
        if self._shadow is not None and getattr(self._pipeline, "repository", None) is not None:
            await self.drain_shadow()
            job_id = self.durable_job_id(int(seq))
            if job_id is None:
                return "missing"
            current = await self._pipeline.repository.get_job(job_id)
            if current is None:
                return "missing"
            if not await self._pipeline.repository.set_job_destination_profile(
                job_id,
                expected_revision=current.revision,
                profile_id=profile.id,
                snapshot=snapshot,
            ):
                return "stale"
        pending.destination_profile_id = profile.id
        pending.destination_profile_name = profile.name
        pending.destination_profile_snapshot = snapshot
        return "ok"

    def pending_confirmation(self, seq: int, *, user_id: int) -> PendingJob | None:
        pending = self._pipeline.pending.get(int(seq))
        if pending is None or int(pending.user_id or 0) != int(user_id):
            return None
        return pending

    def commit_confirmation(
        self,
        ticket: ConfirmationTicket,
        status: object,
        spoiler: bool,
    ) -> Job:
        pending = ticket.pending
        job = Job(
            seq=ticket.seq,
            kind=pending.kind,
            status=status,
            message=pending.message,
            album=pending.album,
            spoiler=spoiler,
            user_id=pending.user_id,
            texts=pending.texts,
            destination_profile_id=pending.destination_profile_id,
            destination_profile_snapshot=pending.destination_profile_snapshot,
        )
        self._pipeline.enqueue(job)
        if self._shadow is not None:
            self._shadow.transition(ticket.seq, "confirmed", "queued", spoiler=bool(spoiler))
        return job

    def settle_failed_confirmation(self, seq: int) -> None:
        self._pipeline._set_cancelled(seq)
        if self._shadow is not None:
            self._shadow.transition(seq, "confirmation_enqueue_failed", "failed")

    def shadow_published(self, seq: int, ids: list) -> None:
        if self._shadow is not None:
            self._shadow.published(seq, ids)

    def shadow_transition(self, seq: int, event_type: str, state: str, **extra: Any) -> None:
        if self._shadow is not None:
            self._shadow.transition(seq, event_type, state, **extra)

    async def transition_now(self, seq: int, event_type: str, state: str, **extra: Any):
        if self._shadow is None:
            return None
        return await self._shadow.transition_now(seq, event_type, state, **extra)

    def shadow_download_completed(self, seq: int, paths: list[str]) -> None:
        if self._shadow is not None:
            self._shadow.download_completed(seq, paths)

    async def complete_download(self, seq: int, paths: list[str]):
        if self._shadow is None:
            return None
        return await self._shadow.complete_download(seq, paths)

    async def complete_publish(self, seq: int, ids: list):
        if self._shadow is None:
            return None
        return await self._shadow.complete_publish(seq, ids)

    async def checkpoint_publish(self, seq: int, refs: list[tuple[int, int, str]]) -> int:
        if self._shadow is None:
            return 0
        return await self._shadow.checkpoint_publish(seq, refs)

    def bind_recovered(self, seq: int, job_id: int) -> None:
        if self._shadow is not None:
            self._shadow.bind_existing(seq, job_id)

    async def claim_next_download(self, owner: str):
        repository = getattr(self._pipeline, "repository", None)
        if repository is None:
            return None
        return await repository.claim_next_download(owner)

    async def claim_next_publish(self, owner: str):
        repository = getattr(self._pipeline, "repository", None)
        if repository is None:
            return None
        return await repository.claim_next_publish(owner)

    async def durable_payload(self, seq: int):
        repository = getattr(self._pipeline, "repository", None)
        if repository is None or self._shadow is None:
            return None
        job_id = self._shadow.job_ids.get(seq)
        if not job_id:
            return None
        record = await repository.get_job(job_id)
        if record is None:
            return None
        items = await repository.list_job_items(job_id)
        paths = [item.local_path for item in items if item.local_path]
        if not paths or len(paths) != len(items):
            return None
        if record.kind in {"album", "collection"}:
            return paths
        return paths[0] if len(paths) == 1 else paths

    async def set_status_reference(self, seq: int, status: object, *, chat_id: int | None = None) -> None:
        if self._shadow is not None:
            await self._shadow.set_status_reference(seq, status, chat_id=chat_id)

    async def persist_progress(
        self,
        seq: int,
        *,
        received: int,
        total: int,
        item: int,
        items: int,
    ) -> None:
        if self._shadow is not None:
            await self._shadow.persist_progress(
                seq,
                received=received,
                total=total,
                item=item,
                items=items,
            )

    async def record_retry(
        self,
        seq: int,
        *,
        phase: str,
        error_code: str,
        error_message: str,
        retry_count: int,
        next_retry_at: float,
    ) -> None:
        if self._shadow is not None:
            await self._shadow.record_retry(
                seq,
                phase=phase,
                error_code=error_code,
                error_message=error_message,
                retry_count=retry_count,
                next_retry_at=next_retry_at,
            )

    async def heartbeat_claim(self, seq: int, kind: str, owner: str) -> bool:
        if self._shadow is None:
            return False
        return await self._shadow.heartbeat_claim(seq, kind, owner)

    async def interrupt_claim(self, seq: int, kind: str, owner: str) -> bool:
        if self._shadow is None:
            return False
        return await self._shadow.interrupt_claim(seq, kind, owner)

    async def drain_shadow(self) -> None:
        if self._shadow is not None:
            await self._shadow.drain()

    def shadow_accept_legacy(
        self,
        seq: int,
        *,
        kind: str,
        user_id: int,
        state: str,
        message: object = None,
        album: list | None = None,
        texts: list | None = None,
        url: str = "",
        spoiler: bool = False,
        status: object = None,
        status_chat_id: int | None = None,
    ) -> None:
        if self._shadow is not None:
            profiles = getattr(self._pipeline, "destination_profiles", None)
            profile = profiles.current_profile if profiles is not None else None
            self._shadow.accept(
                seq,
                kind=kind,
                user_id=user_id,
                state=state,
                source_kind="url" if url else "telegram",
                message=message,
                album=album,
                texts=list(texts or []),
                source_url=url or None,
                spoiler=spoiler,
                status=status,
                status_chat_id=status_chat_id,
                destination_profile_id=(profile.id if profile is not None else None),
                destination_profile_snapshot=(
                    profiles.current_snapshot() if profiles is not None else None
                ),
            )
