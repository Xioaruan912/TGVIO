from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class JobState(StrEnum):
    RECEIVED = "received"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    PLANNED = "planned"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MediaKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    TEXT = "text"


TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}

# MediaItem.metadata keys shared by the downloader, analyzer and planner.
DOWNLOAD_SKIPPED_KEY = "download_skipped"
DOWNLOAD_SKIPPED_CODE_KEY = "download_skipped_code"


def item_download_skipped(item: "MediaItem") -> bool:
    """Whether one item could not be fetched and was deliberately left out."""

    return bool(item.metadata.get(DOWNLOAD_SKIPPED_KEY))

ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.RECEIVED: {
        JobState.DOWNLOADING,
        JobState.DOWNLOADED,
        JobState.CANCELLED,
        JobState.FAILED,
    },
    JobState.DOWNLOADING: {JobState.DOWNLOADED, JobState.CANCELLED, JobState.FAILED},
    JobState.DOWNLOADED: {JobState.ANALYZING, JobState.CANCELLED, JobState.FAILED},
    JobState.ANALYZING: {JobState.ANALYZED, JobState.CANCELLED, JobState.FAILED},
    JobState.ANALYZED: {JobState.PLANNED, JobState.CANCELLED, JobState.FAILED},
    JobState.PLANNED: {JobState.PUBLISHING, JobState.CANCELLED, JobState.FAILED},
    JobState.PUBLISHING: {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED},
    JobState.SUCCEEDED: set(),
    JobState.FAILED: {JobState.RECEIVED, JobState.PLANNED},
    JobState.CANCELLED: set(),
}


@dataclass(frozen=True, slots=True)
class MediaItem:
    index: int
    kind: MediaKind
    source: str
    caption: str = ""
    local_path: str | None = None
    name: str | None = None
    size_bytes: int = 0
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    container: str | None = None
    codec: str | None = None
    spoiler: bool = False
    grouped_id: int | None = None
    source_chat_id: int | None = None
    source_message_id: int | None = None
    sha256: str | None = None
    telegram_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("media index must be >= 0")
        if self.size_bytes < 0:
            raise ValueError("media size must be >= 0")


@dataclass(frozen=True, slots=True)
class JobEvent:
    id: int | None
    job_id: str
    event_type: str
    from_state: JobState | None
    to_state: JobState
    created_at: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class Job:
    owner_id: int
    items: list[MediaItem]
    destination: str
    id: str = field(default_factory=lambda: uuid4().hex)
    state: JobState = JobState.RECEIVED
    policy: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def transition_to(self, next_state: JobState) -> None:
        if next_state not in ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"illegal transition: {self.state.value} -> {next_state.value}")
        self.state = next_state

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES
