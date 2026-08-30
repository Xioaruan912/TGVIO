"""Explicit short-lived interaction state for input-style Telegram settings."""

from __future__ import annotations

from dataclasses import dataclass
import time


@dataclass(frozen=True)
class InteractionSession:
    user_id: int
    kind: str
    field: str
    expires_at: float | None
    revision: int


class InteractionSessions:
    """In-memory interaction registry.

    R1 keeps the same process-local lifetime as the legacy ``*_waiting`` dicts,
    but makes the interaction kind/field/revision explicit so handlers no
    longer coordinate through unrelated pipeline dictionaries.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, InteractionSession] = {}
        self._revision = 0

    def start(
        self,
        user_id: int,
        kind: str,
        field: str,
        *,
        ttl: float | None = None,
    ) -> InteractionSession:
        self._revision += 1
        expires_at = time.time() + ttl if ttl else None
        session = InteractionSession(
            user_id=user_id,
            kind=kind,
            field=field,
            expires_at=expires_at,
            revision=self._revision,
        )
        self._sessions[user_id] = session
        return session

    def get(self, user_id: int, kind: str | None = None) -> InteractionSession | None:
        session = self._sessions.get(user_id)
        if session is None:
            return None
        if session.expires_at is not None and session.expires_at <= time.time():
            self._sessions.pop(user_id, None)
            return None
        if kind is not None and session.kind != kind:
            return None
        return session

    def cancel(self, user_id: int, kind: str | None = None) -> bool:
        session = self.get(user_id)
        if session is None or (kind is not None and session.kind != kind):
            return False
        self._sessions.pop(user_id, None)
        return True

    def finish(self, user_id: int, revision: int | None = None) -> bool:
        session = self.get(user_id)
        if session is None:
            return False
        if revision is not None and session.revision != revision:
            return False
        self._sessions.pop(user_id, None)
        return True

