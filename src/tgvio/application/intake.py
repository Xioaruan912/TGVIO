from __future__ import annotations

from dataclasses import dataclass, field, replace
import time
from typing import Any, Sequence

from tgvio.application.ports import JobRepository
from tgvio.domain.intake import (
    CollectionEntry,
    CollectionEntryKind,
    CollectionSession,
    IntakeEventKey,
    SpoilerMode,
    UserPreference,
)
from tgvio.domain.job import Job, MediaItem, MediaKind


@dataclass(frozen=True, slots=True)
class IncomingMedia:
    kind: MediaKind
    source: str
    caption: str = ""
    size_bytes: int = 0
    name: str | None = None
    spoiler: bool = False
    grouped_id: int | None = None
    source_chat_id: int | None = None
    source_message_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IntakeAcceptResult:
    job: Job
    created: bool


@dataclass(frozen=True, slots=True)
class CollectionFinalizeResult:
    session: CollectionSession
    jobs: tuple[IntakeAcceptResult, ...]
    media_count: int
    text_count: int


class CollectionEmptyError(ValueError):
    pass


class IntakeService:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> JobRepository:
        return self._repository

    async def accept(
        self,
        *,
        owner_id: int,
        destination: str,
        media: Sequence[IncomingMedia],
        policy: dict[str, Any] | None = None,
        spoiler_mode: SpoilerMode | None = None,
        ask_timeout_seconds: int = 60,
    ) -> Job:
        result = await self.accept_once(
            owner_id=owner_id,
            destination=destination,
            media=media,
            policy=policy,
            spoiler_mode=spoiler_mode,
            ask_timeout_seconds=ask_timeout_seconds,
        )
        return result.job

    async def accept_once(
        self,
        *,
        owner_id: int,
        destination: str,
        media: Sequence[IncomingMedia],
        policy: dict[str, Any] | None = None,
        spoiler_mode: SpoilerMode | None = None,
        ask_timeout_seconds: int = 60,
    ) -> IntakeAcceptResult:
        incoming = self._dedupe_batch(media)
        if not incoming:
            raise ValueError("incoming media batch must not be empty")

        # Compatibility for narrow test doubles and any non-SQL repository.
        if not hasattr(self._repository, "lookup_intake_events") or not hasattr(
            self._repository,
            "create_with_intake_events",
        ):
            job = self._build_job(
                owner_id=owner_id,
                destination=destination,
                media=incoming,
                policy=policy,
                spoiler_mode=spoiler_mode,
                ask_timeout_seconds=ask_timeout_seconds,
            )
            await self._repository.create(job)
            return IntakeAcceptResult(job=job, created=True)

        # The unique intake key is enforced in the same transaction as Job
        # creation. A conflict can only happen if another caller won between
        # lookup and create; in that case re-read and filter once more.
        for _attempt in range(4):
            keyed = {
                self._event_key(item): item
                for item in incoming
                if self._event_key(item) is not None
            }
            existing = await self._repository.lookup_intake_events(
                tuple(key for key in keyed if key is not None)
            )
            filtered = [
                item
                for item in incoming
                if (key := self._event_key(item)) is None or key not in existing
            ]
            if not filtered:
                first_key = next(
                    (
                        self._event_key(item)
                        for item in incoming
                        if self._event_key(item) in existing
                    ),
                    None,
                )
                if first_key is None:
                    raise RuntimeError("intake dedupe produced no new or existing event")
                existing_job = await self._repository.get(existing[first_key])
                if existing_job is None:
                    raise RuntimeError("intake event points to missing job")
                return IntakeAcceptResult(job=existing_job, created=False)

            job = self._build_job(
                owner_id=owner_id,
                destination=destination,
                media=filtered,
                policy=policy,
                spoiler_mode=spoiler_mode,
                ask_timeout_seconds=ask_timeout_seconds,
            )
            events = tuple(
                (key, index)
                for index, item in enumerate(filtered)
                if (key := self._event_key(item)) is not None
            )
            if await self._repository.create_with_intake_events(job, events):
                return IntakeAcceptResult(job=job, created=True)
        raise RuntimeError("intake event contention did not converge")

    async def begin_collection(self, *, owner_id: int, chat_id: int) -> CollectionSession:
        existing = await self._repository.get_open_collection(owner_id, chat_id)
        if existing is not None:
            return existing
        return await self._repository.create_collection(
            CollectionSession(owner_id=owner_id, chat_id=chat_id)
        )

    async def open_collection(
        self,
        *,
        owner_id: int,
        chat_id: int,
    ) -> CollectionSession | None:
        return await self._repository.get_open_collection(owner_id, chat_id)

    async def add_collection_media(
        self,
        session: CollectionSession,
        media: Sequence[IncomingMedia],
    ) -> int:
        entries = tuple(
            CollectionEntry(
                session_id=session.id,
                ordinal=-1,
                kind=CollectionEntryKind.MEDIA,
                source_chat_id=item.source_chat_id,
                source_message_id=item.source_message_id,
                payload=self._media_payload(item),
            )
            for item in media
        )
        inserted = await self._repository.append_collection_entries(session.id, entries)
        return len(inserted)

    async def add_collection_text(
        self,
        session: CollectionSession,
        *,
        text: str,
        source_chat_id: int,
        source_message_id: int,
    ) -> bool:
        inserted = await self._repository.append_collection_entries(
            session.id,
            (
                CollectionEntry(
                    session_id=session.id,
                    ordinal=-1,
                    kind=CollectionEntryKind.TEXT,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    payload={"text": text},
                ),
            ),
        )
        return bool(inserted)

    async def collection_counts(self, session_id: str) -> tuple[int, int]:
        entries = await self._repository.list_collection_entries(session_id)
        media_count = sum(entry.kind == CollectionEntryKind.MEDIA for entry in entries)
        text_count = sum(entry.kind == CollectionEntryKind.TEXT for entry in entries)
        return int(media_count), int(text_count)

    async def finalize_collection(
        self,
        *,
        owner_id: int,
        chat_id: int,
        destination: str,
        max_items: int,
        spoiler_mode: SpoilerMode,
        ask_timeout_seconds: int = 60,
    ) -> CollectionFinalizeResult:
        session = await self._repository.get_open_collection(owner_id, chat_id)
        if session is None:
            raise CollectionEmptyError("no open collection")
        entries = await self._repository.list_collection_entries(session.id)
        media_entries = [entry for entry in entries if entry.kind == CollectionEntryKind.MEDIA]
        text_entries = [entry for entry in entries if entry.kind == CollectionEntryKind.TEXT]
        if not media_entries:
            raise CollectionEmptyError("collection contains no media")

        media = [self._media_from_payload(entry.payload) for entry in media_entries]
        collection_caption = self._join_collection_texts(text_entries)
        chunk_size = max(1, int(max_items))
        chunks = [media[start : start + chunk_size] for start in range(0, len(media), chunk_size)]
        jobs: list[IntakeAcceptResult] = []
        for part_index, chunk in enumerate(chunks):
            policy: dict[str, Any] = {
                "collection_id": session.id,
                "collection_part_index": part_index,
                "collection_part_count": len(chunks),
                "display_expected": True,
            }
            if part_index == 0 and collection_caption:
                policy["collection_caption"] = collection_caption
            result = await self.accept_once(
                owner_id=owner_id,
                destination=destination,
                media=chunk,
                policy=policy,
                spoiler_mode=spoiler_mode,
                ask_timeout_seconds=ask_timeout_seconds,
            )
            jobs.append(result)

        finalized = await self._repository.finalize_collection(
            session.id,
            tuple(result.job.id for result in jobs),
        )
        return CollectionFinalizeResult(
            session=finalized,
            jobs=tuple(jobs),
            media_count=len(media_entries),
            text_count=len(text_entries),
        )

    async def cancel_collection(self, *, owner_id: int, chat_id: int) -> CollectionSession | None:
        session = await self._repository.get_open_collection(owner_id, chat_id)
        if session is None:
            return None
        return await self._repository.cancel_collection(session.id)

    async def get_user_preference(self, owner_id: int) -> UserPreference:
        return await self._repository.get_user_preference(owner_id)

    async def set_spoiler_mode(self, owner_id: int, mode: SpoilerMode) -> UserPreference:
        return await self._repository.set_user_spoiler_mode(owner_id, mode)

    async def apply_spoiler_decision(self, job: Job, *, spoiler: bool) -> Job:
        decision = "spoiler" if spoiler else "normal"
        job.items = [replace(item, spoiler=spoiler) for item in job.items]
        job.policy = {
            **job.policy,
            "spoiler_decision": decision,
            "spoiler_decided_at_epoch": int(time.time()),
        }
        job.policy.pop("spoiler_deadline_epoch", None)
        await self._repository.save(job)
        return job

    @staticmethod
    def is_spoiler_pending(job: Job) -> bool:
        return str(job.policy.get("spoiler_decision", "")) == "pending"

    def _build_job(
        self,
        *,
        owner_id: int,
        destination: str,
        media: Sequence[IncomingMedia],
        policy: dict[str, Any] | None,
        spoiler_mode: SpoilerMode | None,
        ask_timeout_seconds: int,
    ) -> Job:
        job_policy = dict(policy or {})
        override: bool | None = None
        if spoiler_mode is not None:
            job_policy["spoiler_mode"] = spoiler_mode.value
            if spoiler_mode == SpoilerMode.ALWAYS_SPOILER:
                override = True
                job_policy["spoiler_decision"] = "spoiler"
            elif spoiler_mode == SpoilerMode.ALWAYS_NORMAL:
                override = False
                job_policy["spoiler_decision"] = "normal"
            elif spoiler_mode == SpoilerMode.ASK:
                job_policy["spoiler_decision"] = "pending"
                job_policy["spoiler_deadline_epoch"] = int(time.time()) + max(
                    1,
                    int(ask_timeout_seconds),
                )
            else:
                job_policy["spoiler_decision"] = "source"

        items = [
            MediaItem(
                index=index,
                kind=item.kind,
                source=item.source,
                caption=item.caption,
                size_bytes=item.size_bytes,
                name=item.name,
                spoiler=item.spoiler if override is None else override,
                grouped_id=item.grouped_id,
                source_chat_id=item.source_chat_id,
                source_message_id=item.source_message_id,
                metadata=dict(item.metadata),
            )
            for index, item in enumerate(media)
        ]
        return Job(
            owner_id=owner_id,
            destination=destination,
            items=items,
            policy=job_policy,
        )

    @staticmethod
    def _event_key(item: IncomingMedia) -> IntakeEventKey | None:
        if item.source_chat_id is None or item.source_message_id is None:
            return None
        return IntakeEventKey(int(item.source_chat_id), int(item.source_message_id))

    @classmethod
    def _dedupe_batch(cls, media: Sequence[IncomingMedia]) -> list[IncomingMedia]:
        seen: set[IntakeEventKey] = set()
        result: list[IncomingMedia] = []
        for item in media:
            key = cls._event_key(item)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def _media_payload(item: IncomingMedia) -> dict[str, Any]:
        return {
            "kind": item.kind.value,
            "source": item.source,
            "caption": item.caption,
            "size_bytes": item.size_bytes,
            "name": item.name,
            "spoiler": item.spoiler,
            "grouped_id": item.grouped_id,
            "source_chat_id": item.source_chat_id,
            "source_message_id": item.source_message_id,
            "metadata": dict(item.metadata),
        }

    @staticmethod
    def _media_from_payload(payload: dict[str, Any]) -> IncomingMedia:
        return IncomingMedia(
            kind=MediaKind(str(payload["kind"])),
            source=str(payload["source"]),
            caption=str(payload.get("caption", "") or ""),
            size_bytes=int(payload.get("size_bytes", 0) or 0),
            name=payload.get("name"),
            spoiler=bool(payload.get("spoiler", False)),
            grouped_id=payload.get("grouped_id"),
            source_chat_id=payload.get("source_chat_id"),
            source_message_id=payload.get("source_message_id"),
            metadata=dict(payload.get("metadata", {}) or {}),
        )

    @staticmethod
    def _join_collection_texts(entries: Sequence[CollectionEntry]) -> str:
        lines: list[str] = []
        for entry in entries:
            text = str(entry.payload.get("text", "") or "")
            lines.extend(line.strip() for line in text.splitlines() if line.strip())
        return "\n".join(lines)
