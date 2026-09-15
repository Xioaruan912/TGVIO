from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class SpoilerMode(StrEnum):
    SOURCE = "source"
    ASK = "ask"
    ALWAYS_SPOILER = "always_spoiler"
    ALWAYS_NORMAL = "always_normal"


class CollectionState(StrEnum):
    OPEN = "open"
    FINALIZED = "finalized"
    CANCELLED = "cancelled"


class CollectionAlreadySubmittedError(ValueError):
    """The frozen collection cannot accept additional entries."""


class CollectionEntryKind(StrEnum):
    MEDIA = "media"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class IntakeEventKey:
    source_chat_id: int
    source_message_id: int


@dataclass(frozen=True, slots=True)
class CollectionEntry:
    session_id: str
    ordinal: int
    kind: CollectionEntryKind
    payload: dict[str, Any] = field(default_factory=dict)
    source_chat_id: int | None = None
    source_message_id: int | None = None
    id: int | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionSession:
    owner_id: int
    chat_id: int
    id: str = field(default_factory=lambda: uuid4().hex)
    state: CollectionState = CollectionState.OPEN
    status_chat_id: int | None = None
    status_message_id: int | None = None
    finalized_job_ids: tuple[str, ...] = ()
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class UserPreference:
    owner_id: int
    spoiler_mode: SpoilerMode = SpoilerMode.SOURCE
    quiet_mode: bool = False
    style_json: str | None = None
    thumbnail_path: str | None = None
    caption_template: str | None = None
    ytdlp_preset: str | None = None
    ytdlp_audio_only: bool = False
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionPreview:
    """Read-only projection of an open collection, before any Job is created."""

    media_count: int
    photo_count: int
    video_count: int
    document_count: int
    total_bytes: int
    cover_plan: str
    discussion_groups: int
    caption_lines: int
    caption_chars: int


@dataclass(frozen=True, slots=True)
class JobDisplayMessage:
    job_id: str
    chat_id: int
    message_id: int
    replacement_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
