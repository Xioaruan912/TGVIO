"""Short-lived destructive-operation confirmation tokens."""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import time


@dataclass(frozen=True)
class PendingOperation:
    operation_id: int
    user_id: int
    action: str
    job_id: int
    expected_revision: int
    expires_at: float
    targets: tuple[tuple[int, int], ...] = ()
    total_bytes: int = 0


class OperationStore:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = float(ttl_seconds)
        self._items: dict[int, PendingOperation] = {}

    def create(self, *, user_id: int, action: str, job_id: int, expected_revision: int) -> PendingOperation:
        self._purge()
        while True:
            operation_id = secrets.randbelow(900_000_000) + 100_000_000
            if operation_id not in self._items:
                break
        item = PendingOperation(
            operation_id=operation_id,
            user_id=int(user_id),
            action=str(action),
            job_id=int(job_id),
            expected_revision=int(expected_revision),
            expires_at=time.time() + self._ttl,
        )
        self._items[operation_id] = item
        return item

    def create_batch(
        self,
        *,
        user_id: int,
        action: str,
        targets: list[tuple[int, int]],
        total_bytes: int = 0,
    ) -> PendingOperation:
        self._purge()
        while True:
            operation_id = secrets.randbelow(900_000_000) + 100_000_000
            if operation_id not in self._items:
                break
        item = PendingOperation(
            operation_id=operation_id,
            user_id=int(user_id),
            action=str(action),
            job_id=0,
            expected_revision=0,
            expires_at=time.time() + self._ttl,
            targets=tuple((int(job_id), int(revision)) for job_id, revision in targets),
            total_bytes=max(0, int(total_bytes)),
        )
        self._items[operation_id] = item
        return item

    def peek(self, operation_id: int, *, user_id: int) -> PendingOperation | None:
        self._purge()
        item = self._items.get(int(operation_id))
        if item is None or item.user_id != int(user_id):
            return None
        return item

    def consume(self, operation_id: int, *, user_id: int) -> PendingOperation | None:
        item = self.peek(operation_id, user_id=user_id)
        if item is None:
            return None
        self._items.pop(item.operation_id, None)
        return item

    def discard(self, operation_id: int, *, user_id: int) -> bool:
        item = self.peek(operation_id, user_id=user_id)
        if item is None:
            return False
        self._items.pop(item.operation_id, None)
        return True

    def _purge(self) -> None:
        now = time.time()
        expired = [key for key, item in self._items.items() if item.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)
