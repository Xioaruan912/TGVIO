from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PreviewState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    id: str
    session_id: str
    owner_id: int
    chat_id: int
    revision: int
    state: PreviewState = PreviewState.PENDING
    cover_entry_id: int | None = None
    cache_dir: str | None = None
    error_code: str | None = None
    expires_at: int = 0
    created_at: str | None = None
    updated_at: str | None = None
