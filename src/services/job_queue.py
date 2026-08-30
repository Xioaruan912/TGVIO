"""R1 queue facade isolating Telegram handlers from pipeline internals."""

from __future__ import annotations

from dataclasses import dataclass
import os
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

    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline

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
        return await self._pipeline._cancel_pending(seq)

    async def cancel(self, seq: int) -> bool:
        return await self._pipeline._cancel_seq(seq)

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
        return self._pipeline.jobs.get(seq)

    def resume(self, seq: int) -> Job | None:
        self._pipeline._paused_files.discard(seq)
        return self._pipeline.jobs.get(seq)

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
        return job

    def settle_failed_confirmation(self, seq: int) -> None:
        self._pipeline._set_cancelled(seq)

