from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class PublishTarget(StrEnum):
    CHANNEL = "channel"
    DISCUSSION = "discussion"


class PublishStepKind(StrEnum):
    CHANNEL_COVER_ALBUM = "channel_cover_album"
    CHANNEL_VIDEO_COVER = "channel_video_cover"
    CHANNEL_MEDIA_GROUP = "channel_media_group"
    CHANNEL_DOCUMENT = "channel_document"
    DISCUSSION_PHOTO_ALBUM = "discussion_photo_album"
    DISCUSSION_VIDEO_ALBUM = "discussion_video_album"
    DISCUSSION_MEDIA = "discussion_media"
    DISCUSSION_DOCUMENT = "discussion_document"


class PublishStepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PublishStep:
    index: int
    kind: PublishStepKind
    target: PublishTarget
    item_indexes: tuple[int, ...]
    params: dict[str, Any] = field(default_factory=dict)
    state: PublishStepState = PublishStepState.PENDING
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class PublishPlan:
    job_id: str
    steps: tuple[PublishStep, ...]
    summary: dict[str, Any]
    id: str = field(default_factory=lambda: uuid4().hex)
    version: int = 2
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class PublishEffect:
    plan_id: str
    step_index: int
    effect_type: str
    external_chat_id: str | None = None
    external_message_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    effect_type: str
    external_chat_id: str | None = None
    external_message_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

