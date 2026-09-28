from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tgvio_player.domain.ranges import ByteRange


@dataclass(frozen=True, slots=True)
class StartupCacheKey:
    media_id: str
    remote_etag: str | None
    byte_range: ByteRange


class StartupRangeCache:
    """Byte-bounded LRU startup cache with shareable in-flight fetches.

    Callers acquire a lease and must release it.  Cancelling one consumer does
    not cancel a shared fetch; the upstream task is cancelled only after the
    last lease is released before completion.
    """

    def __init__(self, *, max_entries: int, max_bytes: int) -> None:
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("startup cache limits must be positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[StartupCacheKey, bytes] = OrderedDict()
        self._bytes = 0
        self._inflight: dict[StartupCacheKey, tuple[asyncio.Task[bytes], int]] = {}
        self._lock = asyncio.Lock()

    @property
    def byte_size(self) -> int:
        return self._bytes

    async def acquire(
        self,
        key: StartupCacheKey,
        fetch: Callable[[], Awaitable[bytes]],
    ) -> tuple[bytes | None, asyncio.Task[bytes] | None]:
        async with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached, None
            active = self._inflight.get(key)
            if active is not None:
                task, leases = active
                if not task.done() and not task.cancelling():
                    self._inflight[key] = (task, leases + 1)
                    return None, task
                # A last released lease may have cancelled this task before its
                # completion callback runs. Do not hand that cancellation to a
                # new consumer for the same startup range.
                self._inflight.pop(key, None)
            task = asyncio.create_task(fetch())
            self._inflight[key] = (task, 1)
            task.add_done_callback(lambda done: asyncio.create_task(self._complete(key, done)))
            return None, task

    async def release(self, key: StartupCacheKey) -> None:
        async with self._lock:
            active = self._inflight.get(key)
            if active is None:
                return
            task, leases = active
            if leases <= 1:
                if not task.done():
                    task.cancel()
                self._inflight[key] = (task, 0)
            else:
                self._inflight[key] = (task, leases - 1)

    async def discard(self, media_id: str) -> None:
        async with self._lock:
            for key in [item for item in self._entries if item.media_id == media_id]:
                payload = self._entries.pop(key)
                self._bytes -= len(payload)
            for key in [item for item in self._inflight if item.media_id == media_id]:
                task, _leases = self._inflight.pop(key)
                if not task.done():
                    task.cancel()

    async def _complete(self, key: StartupCacheKey, task: asyncio.Task[bytes]) -> None:
        try:
            payload = task.result()
        except (asyncio.CancelledError, Exception):
            payload = None
        async with self._lock:
            active = self._inflight.get(key)
            if active is not None and active[0] is not task:
                return
            self._inflight.pop(key, None)
            if payload is not None and len(payload) <= self._max_bytes:
                previous = self._entries.pop(key, None)
                if previous is not None:
                    self._bytes -= len(previous)
                self._entries[key] = payload
                self._bytes += len(payload)
                while self._bytes > self._max_bytes or len(self._entries) > self._max_entries:
                    _, evicted = self._entries.popitem(last=False)
                    self._bytes -= len(evicted)
