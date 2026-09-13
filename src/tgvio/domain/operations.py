from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RevocationState(StrEnum):
    PENDING = "pending"
    DELETED = "deleted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OperationToken:
    token: str
    owner_id: int
    action: str
    resource_type: str
    resource_id: str
    expected_revision: int
    payload_hash: str
    payload: dict[str, Any] = field(default_factory=dict)
    expires_at: int = 0
    consumed_at: int | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class PublishEffectRevocation:
    effect_id: int
    state: RevocationState = RevocationState.PENDING
    attempt_count: int = 0
    error_code: str | None = None
    deleted_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class UndoTarget:
    effect_ids: tuple[int, ...]
    peer_id: int
    message_id: int
    effect_type: str


@dataclass(frozen=True, slots=True)
class UndoStatus:
    job_id: str
    total_messages: int
    deleted_messages: int
    failed_messages: int
    channel_messages: int
    discussion_messages: int
    remaining_channel_messages: int
    remaining_discussion_messages: int
    remaining_targets: tuple[UndoTarget, ...]
    expected_revision: int
    payload_hash: str

    @property
    def remaining_messages(self) -> int:
        return len(self.remaining_targets)

    @property
    def complete(self) -> bool:
        return self.total_messages > 0 and self.remaining_messages == 0


@dataclass(frozen=True, slots=True)
class UndoConfirmation:
    operation: OperationToken
    status: UndoStatus


@dataclass(frozen=True, slots=True)
class UndoResult:
    job_id: str
    total_messages: int
    deleted_now: int
    deleted_total: int
    failed_now: int
    remaining_messages: int

    @property
    def complete(self) -> bool:
        return self.total_messages > 0 and self.remaining_messages == 0
