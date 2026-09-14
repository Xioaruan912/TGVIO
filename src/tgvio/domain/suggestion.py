from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SuggestionKind(StrEnum):
    COVER = "cover"
    ORDER = "order"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class Suggestion:
    kind: SuggestionKind
    title: str
    detail: str
    entry_ids: tuple[int, ...] = ()
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class SuggestionApplication:
    id: str
    session_id: str
    owner_id: int
    kind: str
    revision_applied: int
    before_json: str
    after_json: str
    consumed: bool = False
    created_at: str | None = None
