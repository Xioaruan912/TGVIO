from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
import time
from typing import Any, Sequence

from tgvio.application.ports import JobRepository
from tgvio.domain.intake import (
    IntakeEventKey,
    SpoilerMode,
    UserPreference,
)
from tgvio.application.content_prefs import content_policy_snapshot
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.observability import log_event


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


class IntakeService:
    def __init__(
        self,
        repository: JobRepository,
        *,
        default_policy: dict[str, Any] | None = None,
    ) -> None:
        self._repository = repository
        self._default_policy = dict(default_policy or {})
        self._log = logging.getLogger("tgvio.intake")

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
        policy = await self._apply_content_policy(owner_id, policy)

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
            releasable = await self._releasable_keys(existing)
            blocked = {
                key: job_id for key, job_id in existing.items() if key not in releasable
            }
            filtered = [
                item
                for item in incoming
                if (key := self._event_key(item)) is None or key not in blocked
            ]
            if not filtered:
                first_key = next(
                    (
                        self._event_key(item)
                        for item in incoming
                        if self._event_key(item) in blocked
                    ),
                    None,
                )
                if first_key is None:
                    raise RuntimeError("intake dedupe produced no new or existing event")
                existing_job = await self._repository.get(blocked[first_key])
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
            released = tuple(
                key
                for key in releasable
                if key in keyed
            )
            if await self._repository.create_with_intake_events(
                job, events, release_keys=released
            ):
                if released:
                    log_event(
                        self._log,
                        logging.INFO,
                        "intake.events.released",
                        "Source messages of failed jobs were handed to a new Job",
                        job_id=job.id,
                        released=len(released),
                    )
                return IntakeAcceptResult(job=job, created=True)
            # The transaction refused the hand-off because another caller won,
            # or because the failed Job acquired a visible publish effect after
            # our initial lookup. In both cases return the durable owner rather
            # than repeatedly trying to release the same source key.
            current = await self._repository.lookup_intake_events(
                tuple(key for key, _item_index in events)
            )
            if current:
                existing_job = await self._repository.get(next(iter(current.values())))
                if existing_job is not None:
                    return IntakeAcceptResult(job=existing_job, created=False)
        raise RuntimeError("intake event contention did not converge")

    async def _releasable_keys(
        self,
        existing: dict[IntakeEventKey, str],
    ) -> set[IntakeEventKey]:
        """Keys whose Job failed or was cancelled, so they may be grabbed again."""

        if not existing:
            return set()
        states: dict[str, JobState] = {}
        releasable: set[IntakeEventKey] = set()
        for key, job_id in existing.items():
            state = states.get(job_id)
            if state is None:
                job = await self._repository.get(job_id)
                if job is None:
                    # A dangling event row must never block the owner forever.
                    releasable.add(key)
                    continue
                state = job.state
                states[job_id] = state
            if state in {JobState.FAILED, JobState.CANCELLED}:
                releasable.add(key)
        return releasable

    async def _apply_content_policy(
        self,
        owner_id: int,
        policy: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = dict(policy or {})
        getter = getattr(self._repository, "get_user_preference", None)
        if getter is None:
            return merged
        try:
            preference = await getter(int(owner_id))
        except Exception:
            return merged
        snapshot = content_policy_snapshot(preference)
        for key in ("thumbnail_path", "caption_template", "ytdlp"):
            if key not in merged and snapshot.get(key) is not None:
                merged[key] = snapshot[key]
        return merged

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
        job_policy = dict(self._default_policy)
        job_policy.update(policy or {})
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

def _ceil_div(value: int, divisor: int) -> int:
    if divisor <= 0:
        return 0
    return (max(0, int(value)) + divisor - 1) // divisor
