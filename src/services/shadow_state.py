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
            "metadata": {"schema_version": 1},
        }

    async def _accept(self, seq: int, **kwargs: Any) -> None:
        message = kwargs.pop("message", None)
        album = kwargs.pop("album", None)
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
            state = kwargs.get("state")
            await self.repository.record_job_event(
                job_id,
                "shadow_update",
                to_state=state,
                payload={"schema_version": 1, "legacy_seq": seq},
            )
            return
        extra = dict(kwargs.pop("event_extra", {}) or {})
        extra.update({"schema_version": 1, "legacy_seq": seq})
        record = await self.repository.accept_job(
            items=items,
            event_payload=extra,
            **kwargs,
        )
        self.job_ids[seq] = record.id
        self._seen_items[seq] = {
            (item.get("source_chat_id"), item.get("source_message_id")) for item in items
        }

    def accept(self, seq: int, **kwargs: Any) -> None:
        self._schedule(lambda: self._accept(seq, **kwargs), f"accept #{seq}")

    def transition(self, seq: int, event_type: str, state: str | None, **extra: Any) -> None:
        async def run() -> None:
            job_id = self.job_ids.get(seq)
            if not job_id:
                return
            await self.repository.record_job_event(
                job_id,
                event_type,
                to_state=state,
                payload={"schema_version": 1, "legacy_seq": seq, **extra},
            )
        self._schedule(run, f"transition #{seq} {event_type}")

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
            await self.repository.record_published_messages(job_id, refs)
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
