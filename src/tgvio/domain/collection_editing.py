from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from tgvio.domain.intake import CollectionEntry


class DraftState(StrEnum):
    COLLECTING = "collecting"
    PREVIEW = "preview"
    SAVED = "saved"
    SUBMITTED = "submitted"
    DISCARDED = "discarded"


class EditingField(StrEnum):
    CAPTION = "caption"


@dataclass(frozen=True, slots=True)
class CollectionDraft:
    session_id: str
    owner_id: int
    chat_id: int
    revision: int = 1
    state: DraftState = DraftState.COLLECTING
    cover_entry_id: int | None = None
    caption_override: str | None = None
    active: bool = True
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class DraftEntry:
    """A collection entry plus its editable overlay (order + excluded flag)."""

    entry: CollectionEntry
    position: int
    excluded: bool = False

    @property
    def entry_id(self) -> int | None:
        return self.entry.id


@dataclass(frozen=True, slots=True)
class DraftSummary:
    session_id: str
    revision: int
    state: DraftState
    media_count: int
    text_count: int
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionSubmission:
    session_id: str
    owner_id: int
    revision: int
    snapshot_hash: str
    job_ids: tuple[str, ...] = ()
    state: str = "creating"
    token_id: str | None = None
    frozen_json: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class EditingInteraction:
    id: str
    owner_id: int
    chat_id: int
    session_id: str
    field: EditingField
    expected_revision: int
    expires_at: int
    consumed_at: int | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenCollection:
    """Immutable snapshot committed at confirm time."""

    session_id: str
    revision: int
    media: tuple[object, ...] = field(default_factory=tuple)
    caption: str = ""
    cover_entry_id: int | None = None
    cover_index: int | None = None
    snapshot_hash: str = ""
