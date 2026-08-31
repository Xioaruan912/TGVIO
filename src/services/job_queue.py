"""R1 queue facade isolating Telegram handlers from pipeline internals."""

from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import time
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
        try:
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
        }

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
        for item in detail.pop("items", []):
            path = item.get("local_path")
            size = int(item.get("size_bytes") or 0)
            if path and os.path.isfile(path):
                try:
                    if size <= 0 or os.path.getsize(path) == size:
                        cache_exists = True
                except OSError:
                    pass
        detail["cache_exists"] = cache_exists
        seq = detail.get("legacy_seq")
        detail["can_retry"] = bool(seq is not None and int(seq) in self._pipeline.retryable)
        backup = detail.get("backup")
        if backup:
            remote_dir = str(backup.get("remote_dir") or "")
            detail["backup_summary"] = os.path.basename(remote_dir.rstrip("/")) or "最近一次尝试"
        else:
            detail["backup_summary"] = ""
        detail["error_message"] = str(detail.get("error_message") or "")[:1000]
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
        if record.legacy_seq is not None and await self.cancel(int(record.legacy_seq)):
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
        ticket = self.claim_retry(int(record.legacy_seq))
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
    ) -> int:
        return await self._pipeline._auto_enqueue(
            kind,
            message,
            album,
            user_id,
            force_normal=force_normal,
            texts=texts,
            reserved_seq=reserved_seq,
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

    async def cancel_pending(self, seq: int) -> bool:
        ok = await self._pipeline._cancel_pending(seq)
        if ok and self._shadow is not None:
            if getattr(self._pipeline, "repository", None) is not None:
                await self._shadow.transition_now(seq, "cancelled", "cancelled")
            else:
                self._shadow.transition(seq, "cancelled", "cancelled")
        return ok

    async def cancel(self, seq: int) -> bool:
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

    def stop_running(self, seq: int) -> bool:
        task = self._pipeline._download_tasks.get(seq)
        if task is None or task.done():
            task = self._pipeline._upload_tasks.get(seq)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def pop_published(self, seq: int):
        return self._pipeline.published.pop(seq, None)

    def hold(self, seq: int) -> Job | None:
        self._pipeline._paused_files.add(seq)
        job = self._pipeline.jobs.get(seq)
        if job is not None and self._shadow is not None:
            self._shadow.transition(seq, "paused", "paused")
        return job

    def resume(self, seq: int) -> Job | None:
        self._pipeline._paused_files.discard(seq)
        job = self._pipeline.jobs.get(seq)
        if job is not None and self._shadow is not None:
            self._shadow.resume(seq)
        return job

    def claim_retry(self, seq: int) -> RetryTicket | None:
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
                event_type="retry_accepted",
                event_extra={"retry_of_legacy_seq": ticket.old_seq},
            )
        return new_job

    def claim_confirmation(self, seq: int) -> ConfirmationTicket | None:
        pending = self._pipeline.pending.pop(seq, None)
        if pending is None:
            return None
        if pending.timeout_task is not None:
            pending.timeout_task.cancel()
        self._pipeline.active_seqs.add(seq)
        return ConfirmationTicket(seq=seq, pending=pending)

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
        items = await repository.list_job_items(job_id)
        paths = [item.local_path for item in items if item.local_path]
        if not paths or len(paths) != len(items):
            return None
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
            )
