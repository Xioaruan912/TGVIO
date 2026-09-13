from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import time
from typing import Any
from uuid import uuid4


class OutboxState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SENT = "sent"
    DEAD = "dead"


# Only these stable event summaries may leave the process. They never contain
# captions, user/peer identifiers, URLs, local paths, or credentials.
ALLOWED_NOTIFICATION_EVENTS = {
    "job.succeeded",
    "job.failed",
    "job.cancelled",
    "archive.committed",
    "archive.failed",
    "runtime.started",
    "runtime.telegram_disconnected",
    "runtime.telegram_recovered",
    "runtime.disk_low",
    "runtime.disk_recovered",
}

# Only failures/anomalies (and their recovery) are pushed to the owner's private
# chat. Success events stay in the outbox for other delivery channels.
ALERT_EVENT_TYPES = frozenset(
    {
        "job.failed",
        "archive.failed",
        "runtime.telegram_disconnected",
        "runtime.telegram_recovered",
        "runtime.disk_low",
        "runtime.disk_recovered",
    }
)

ALLOWED_PAYLOAD_KEYS = {
    "job_id",
    "package_id",
    "state",
    "error_code",
    "media_count",
    "bytes",
    "accepted_order",
    "occurred_at",
    "component",
    "condition",
    "free_bytes",
    "reserve_bytes",
}


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    event_type: str
    dedupe_key: str
    payload: dict[str, Any]
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if self.event_type not in ALLOWED_NOTIFICATION_EVENTS:
            raise ValueError(f"notification event is not allowlisted: {self.event_type}")
        if not self.dedupe_key:
            raise ValueError("notification dedupe key must not be empty")
        if self.max_attempts < 1:
            raise ValueError("notification max attempts must be >= 1")
        for key in self.payload:
            if key not in ALLOWED_PAYLOAD_KEYS:
                raise ValueError(f"notification payload key is not allowlisted: {key}")


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    id: int
    event_type: str
    dedupe_key: str
    payload: dict[str, Any]
    state: OutboxState
    attempts: int
    max_attempts: int
    next_attempt_at: float
    claimed_by: str | None = None
    claim_expires_at: float | None = None
    last_error_code: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    sent_at: float | None = None

    @property
    def delivery_signature(self) -> dict[str, Any]:
        return {"event": self.event_type, **self.payload}


def retry_delay_seconds(
    attempts: int,
    *,
    base_seconds: float = 30.0,
    cap_seconds: float = 3600.0,
    jitter: float = 0.0,
) -> float:
    if attempts < 1:
        attempts = 1
    delay = min(cap_seconds, base_seconds * (2 ** (attempts - 1)))
    return max(0.0, delay + jitter)


def new_holder_id() -> str:
    return uuid4().hex


def now_epoch() -> float:
    return time.time()
