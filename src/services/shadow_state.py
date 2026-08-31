"""Best-effort, non-authoritative R2-B mirror of legacy queue state."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ShadowState:
    def __init__(self, repository: Any | None) -> None:
        self.repository = repository
        self.job_ids: dict[int, int] = {}
        self._seen_items: dict[int, set[tuple[int | None, int | None]]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    def _schedule(self, factory, label: str) -> None:
        if self.repository is None:
            return
        async def serial() -> None:
            async with self._lock:
                await factory()

        task = asyncio.get_running_loop().create_task(serial())
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("SQLite shadow write failed: %s", label)

        task.add_done_callback(done)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @staticmethod
    def _item(message: Any) -> dict[str, Any]:
        media = getattr(message, "media", None)
        document = getattr(media, "document", None)
        photo = getattr(media, "photo", None)
        return {
            "source_chat_id": getattr(message, "chat_id", None),
            "source_message_id": getattr(message, "id", None),
            "grouped_id": getattr(message, "grouped_id", None),
            "media_kind": "document" if document is not None else "photo" if photo is not None else None,
            "original_name": getattr(document, "file_name", None),
            "mime_type": getattr(document, "mime_type", None),
            "metadata": {
                "schema_version": 1,
                "caption": str(getattr(message, "message", "") or "")[:1024],
            },
        }

    async def _accept(self, seq: int, **kwargs: Any) -> None:
        message = kwargs.pop("message", None)
        album = kwargs.pop("album", None)
        status = kwargs.pop("status", None)
        status_chat_id = kwargs.pop("status_chat_id", None)
        items = [self._item(m) for m in list(album or ([] if message is None else [message]))]
        if seq in self.job_ids:
            job_id = self.job_ids[seq]
            seen = self._seen_items.setdefault(seq, set())
            new_items = []
            for item in items:
                key = (item.get("source_chat_id"), item.get("source_message_id"))
                if key not in seen:
                    seen.add(key)
                    new_items.append(item)
            if new_items:
                await self.repository.append_job_items(job_id, new_items)
            if status is not None:
                await self.repository.set_status_reference(
                    job_id,
                    chat_id=status_chat_id,
                    message_id=getattr(status, "id", None),
                )
            await self.repository.record_job_event(
                job_id,
                "shadow_update",
                payload={"schema_version": 1, "legacy_seq": seq},
            )
            return
        extra = dict(kwargs.pop("event_extra", {}) or {})
        extra.update({"schema_version": 1, "legacy_seq": seq})
        record = await self.repository.accept_job(
            items=items,
            legacy_seq=seq,
            event_payload=extra,
            **kwargs,
        )
        self.job_ids[seq] = record.id
        if status is not None:
            await self.repository.set_status_reference(
                record.id,
                chat_id=status_chat_id,
                message_id=getattr(status, "id", None),
            )
        self._seen_items[seq] = {
            (item.get("source_chat_id"), item.get("source_message_id")) for item in items
        }

    def accept(self, seq: int, **kwargs: Any) -> None:
        self._schedule(lambda: self._accept(seq, **kwargs), f"accept #{seq}")

    def transition(self, seq: int, event_type: str, state: str | None, **extra: Any) -> None:
        self._schedule(
            lambda: self.transition_now(seq, event_type, state, **extra),
            f"transition #{seq} {event_type}",
        )

    async def transition_now(
        self, seq: int, event_type: str, state: str | None, **extra: Any
    ):
        job_id = self.job_ids.get(seq)
        if not job_id or state is None or self.repository is None:
            return None
        current = await self.repository.get_job(job_id)
        if current is None or current.state == state:
            return None
        return await self.repository.transition_job(
            job_id,
            expected_revision=current.revision,
            to_state=state,
            event_type=event_type,
            payload={"schema_version": 1, "legacy_seq": seq, **extra},
        )

    def download_completed(self, seq: int, paths: list[str]) -> None:
        async def run() -> None:
            job_id = self.job_ids.get(seq)
            if not job_id:
                return
            current = await self.repository.get_job(job_id)
            if current is None:
                return
            await self.repository.record_download_ready(
                job_id,
                list(paths),
                expected_revision=current.revision,
            )
        self._schedule(run, f"download completed #{seq}")

    async def complete_download(self, seq: int, paths: list[str]):
        job_id = self.job_ids.get(seq)
        if not job_id or self.repository is None:
            return None
        current = await self.repository.get_job(job_id)
        if current is None:
            return None
        return await self.repository.record_download_ready(
            job_id,
            list(paths),
            expected_revision=current.revision,
        )

    async def complete_publish(self, seq: int, ids: list):
        job_id = self.job_ids.get(seq)
        if not job_id or self.repository is None:
            return None
        existing = await self.repository.list_published_messages(job_id)
        known_refs = {
            (item.peer_id, item.message_id)
            for item in existing
            if item.deleted_at is None
        }
        refs = []
        for value in ids or []:
            if isinstance(value, tuple) and len(value) >= 2:
                message_id = int(value[1])
                try:
                    peer_id = int(value[0])
                except (TypeError, ValueError):
                    peer_id = 0
                if (peer_id, message_id) in known_refs:
                    continue
                role = str(value[2]) if len(value) >= 3 else "published"
                refs.append((peer_id, message_id, role))
            else:
                message_id = int(value)
                if (0, message_id) not in known_refs:
                    refs.append((0, message_id, "legacy_destination"))
        current = await self.repository.get_job(job_id)
        if current is None:
            return None
        return await self.repository.record_published_messages(
            job_id,
            refs,
            expected_revision=current.revision,
        )

    async def checkpoint_publish(self, seq: int, refs: list[tuple[int, int, str]]) -> int:
        job_id = self.job_ids.get(seq)
        if not job_id or self.repository is None:
            return 0
        return await self.repository.checkpoint_published_messages(job_id, list(refs))

    def bind_existing(self, seq: int, job_id: int) -> None:
        self.job_ids[int(seq)] = int(job_id)

    async def set_status_reference(self, seq: int, status: Any, *, chat_id: int | None = None) -> None:
        job_id = self.job_ids.get(int(seq))
        if not job_id or self.repository is None or status is None:
            return
        await self.repository.set_status_reference(
            job_id,
            chat_id=chat_id,
            message_id=getattr(status, "id", None),
        )

    async def persist_progress(
        self,
        seq: int,
        *,
        received: int,
        total: int,
        item: int,
        items: int,
    ) -> None:
        job_id = self.job_ids.get(int(seq))
        if not job_id or self.repository is None:
            return
        await self.repository.update_job_progress(
            job_id,
            bytes_done=received,
            bytes_total=total,
            current_item=item,
            total_items=items,
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
        job_id = self.job_ids.get(int(seq))
        if not job_id or self.repository is None:
            return
        await self.repository.record_job_retry(
            job_id,
            phase=phase,
            error_code=error_code,
            error_message=error_message,
            retry_count=retry_count,
            next_retry_at=next_retry_at,
        )

    async def heartbeat_claim(self, seq: int, kind: str, owner: str) -> bool:
        job_id = self.job_ids.get(seq)
        if not job_id or self.repository is None:
            return False
        return await self.repository.heartbeat_claim(
            job_id,
            owner=owner,
            kind=kind,
        )

    async def interrupt_claim(self, seq: int, kind: str, owner: str) -> bool:
        job_id = self.job_ids.get(seq)
        if not job_id or self.repository is None:
            return False
        result = await self.repository.interrupt_claim(
            job_id,
            owner=owner,
            kind=kind,
            reason="graceful_shutdown",
        )
        return result.applied

    def event(self, seq: int, event_type: str, **extra: Any) -> None:
        async def run() -> None:
            job_id = self.job_ids.get(seq)
            if not job_id:
                return
            await self.repository.record_job_event(
                job_id,
                event_type,
                payload={"schema_version": 1, "legacy_seq": seq, **extra},
            )
        self._schedule(run, f"event #{seq} {event_type}")

    def resume(self, seq: int) -> None:
        async def run() -> None:
            job_id = self.job_ids.get(seq)
            if not job_id:
                return
            current = await self.repository.get_job(job_id)
            if current is None or not current.resume_state:
                return
            await self.repository.transition_job(
                job_id,
                expected_revision=current.revision,
                to_state=current.resume_state,
                event_type="resumed",
                payload={"schema_version": 1, "legacy_seq": seq},
            )
        self._schedule(run, f"resume #{seq}")

    def published(self, seq: int, ids: list) -> None:
        async def run() -> None:
            job_id = self.job_ids.get(seq)
            if not job_id:
                return
            refs = []
            for value in ids or []:
                if isinstance(value, tuple) and len(value) >= 2:
                    refs.append((int(value[0]), int(value[1]), "published"))
                else:
                    refs.append((0, int(value), "legacy_destination"))
            current = await self.repository.get_job(job_id)
            if current is None:
                return
            await self.repository.record_published_messages(
                job_id, refs, expected_revision=current.revision
            )
        self._schedule(run, f"published #{seq}")

    def backup_started(self, seq: int, remote_dir: str, files: list[dict[str, Any]]) -> None:
        async def run() -> None:
            job_id = self.job_ids.get(seq)
            if not job_id:
                return
            attempt = await self.repository.create_backup_attempt(
                job_id=job_id, state="running", remote_dir=remote_dir
            )
            for file in files:
                await self.repository.create_backup_file(
                    attempt_id=attempt.id,
                    local_path=str(file["local"]),
                    remote_name=str(file["name"]),
                    size_bytes=int(file.get("size") or 0),
                    state="pending",
                )
        self._schedule(run, f"backup #{seq}")

    async def ensure_backup_file(
        self,
        seq: int,
        remote_dir: str,
        file: dict[str, Any],
    ):
        job_id = self.job_ids.get(int(seq))
        if not job_id or self.repository is None:
            return None, None
        attempt = await self.repository.ensure_backup_attempt(
            job_id=job_id, remote_dir=str(remote_dir)
        )
        record = await self.repository.ensure_backup_file(
            attempt_id=attempt.id,
            local_path=str(file["local"]),
            remote_name=str(file["name"]),
            size_bytes=int(file.get("size") or 0),
        )
        return attempt, record

    async def update_backup_file(
        self,
        file_id: int,
        *,
        state: str,
        bytes_done: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self.repository is None:
            return
        await self.repository.update_backup_file_status(
            int(file_id), state=state, bytes_done=bytes_done,
            error_code=error_code, error_message=error_message,
        )

    async def update_backup_attempt(
        self,
        attempt_id: int,
        *,
        state: str,
        retry_count: int | None = None,
        next_retry_at: float | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        finished: bool = False,
    ) -> None:
        if self.repository is None:
            return
        await self.repository.update_backup_attempt_status(
            int(attempt_id), state=state, retry_count=retry_count,
            next_retry_at=next_retry_at, error_code=error_code,
            error_message=error_message, finished=finished,
        )

    async def backup_retry_due(self, seq: int, remote_dir: str) -> tuple[bool, float | None]:
        if self.repository is None:
            return True, None
        return await self.repository.backup_retry_due(
            legacy_seq=int(seq), remote_dir=str(remote_dir)
        )
