from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import logging

from tgvio_player.domain.ranges import ByteRange

_LOG = logging.getLogger("tgvio_player.rangecache")


class RangeCacheError(RuntimeError):
    """A remote read failed while filling the cache."""


class _AdaptiveGate:
    """Concurrency limiter that shrinks on throttling (403/429/5xx) and recovers."""

    def __init__(self, limit: int) -> None:
        self.max = max(1, int(limit))
        self.limit = self.max
        self.active = 0
        self._condition = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._condition:
            while self.active >= self.limit:
                await self._condition.wait()
            self.active += 1

    async def release(self) -> None:
        async with self._condition:
            self.active = max(0, self.active - 1)
            self._condition.notify()

    async def penalize(self) -> None:
        async with self._condition:
            self.limit = max(1, self.limit - 1)

    async def reward(self) -> None:
        async with self._condition:
            if self.limit < self.max:
                self.limit += 1
                self._condition.notify()


class MediaRangeCache:
    """Streams media ranges through a bounded on-disk chunk cache.

    Upstream reads happen in large aligned *windows* (default 32 MB) so the slow
    per-request latency of the archive backend is amortised over many chunks; the
    data is split into local chunks (default 4 MB) as it streams and served from
    disk on later hits. Windows fetch concurrently (default 4) and the limiter
    backs off automatically when the backend throttles with 403/429/5xx.
    """

    def __init__(
        self,
        store: object,
        reader: object,
        *,
        window_bytes: int | None = None,
        concurrency: int = 4,
        should_pause: Callable[[], bool] | None = None,
        max_attempts: int = 4,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._store = store
        self._reader = reader
        self._chunk_bytes = int(store.chunk_bytes)
        window = int(window_bytes) if window_bytes else self._chunk_bytes * 8
        self._window_chunks = max(1, window // self._chunk_bytes)
        self._window_bytes = self._window_chunks * self._chunk_bytes
        self._gate = _AdaptiveGate(concurrency)
        self._should_pause = should_pause or (lambda: False)
        self._max_attempts = max(1, int(max_attempts))
        self._backoff = max(0.05, float(backoff_seconds))
        self._window_tasks: dict[tuple[str, int], asyncio.Task[object]] = {}
        self._chunk_events: dict[tuple[str, int], asyncio.Event] = {}
        self._failed: dict[str, Exception] = {}
        self._tasks: set[asyncio.Task[object]] = set()

    def open(self) -> None:
        self._store.open()

    def _chunk_bounds(self, size: int, index: int) -> tuple[int, int]:
        start = index * self._chunk_bytes
        end = min(start + self._chunk_bytes, size) - 1
        return start, end

    def _window_for_chunk(self, index: int) -> int:
        return index // self._window_chunks

    async def _chunk(self, key: str, package: str, relpath: str, size: int, index: int) -> bytes:
        cached = self._store.read_slice(key, index, 0, self._chunk_bytes)
        if cached is not None:
            return cached
        event = self._chunk_events.setdefault((key, index), asyncio.Event())
        self._ensure_window(key, package, relpath, size, self._window_for_chunk(index))
        await event.wait()
        failure = self._failed.get(f"{key}:{index}")
        if failure is not None:
            raise RangeCacheError(str(failure))
        cached = self._store.read_slice(key, index, 0, self._chunk_bytes)
        if cached is None:
            raise RangeCacheError("chunk unavailable after fetch")
        return cached

    def _ensure_window(self, key: str, package: str, relpath: str, size: int, window: int) -> None:
        if window * self._window_bytes >= size:
            return
        if (key, window) in self._window_tasks:
            return
        task = asyncio.create_task(self._fetch_window(key, package, relpath, size, window))
        self._window_tasks[(key, window)] = task
        self._tasks.add(task)

        def _done(_task: asyncio.Task[object]) -> None:
            self._window_tasks.pop((key, window), None)
            self._tasks.discard(_task)

        task.add_done_callback(_done)

    async def _fetch_window(
        self, key: str, package: str, relpath: str, size: int, window: int
    ) -> None:
        window_start = window * self._window_bytes
        window_end = min(window_start + self._window_bytes, size) - 1
        first_chunk = window_start // self._chunk_bytes
        last_chunk = window_end // self._chunk_bytes

        attempt = 0
        while attempt < self._max_attempts:
            while self._should_pause():
                await asyncio.sleep(0.2)
            await self._gate.acquire()
            response = None
            try:
                response = await self._reader.open_range(
                    package, relpath, ByteRange(window_start, window_end)
                )
                if response.status in {200, 206}:
                    await self._consume_window(key, size, response, first_chunk, last_chunk)
                    await self._gate.reward()
                    return
                await self._gate.penalize()
            except Exception:
                _LOG.info("player.rangecache.fetch_error key=%s win=%s", key[:12], window)
                await self._gate.penalize()
            finally:
                if response is not None:
                    await self._close(response)
                await self._gate.release()
            attempt += 1
            if attempt >= self._max_attempts:
                break
            await asyncio.sleep(self._backoff * attempt)
        self._mark_failed(key, first_chunk, last_chunk, RangeCacheError("window fetch failed"))

    @staticmethod
    async def _close(response: object) -> None:
        body = getattr(response, "body", None)
        close = getattr(body, "aclose", None)
        if close is not None:
            await close()

    async def _consume_window(
        self,
        key: str,
        size: int,
        response: object,
        first_chunk: int,
        last_chunk: int,
    ) -> None:
        buffer = bytearray()
        chunk_index = first_chunk
        async for piece in response.body:  # type: ignore[attr-defined]
            if not piece:
                continue
            buffer.extend(piece)
            while chunk_index <= last_chunk:
                start = chunk_index * self._chunk_bytes
                expected = min(self._chunk_bytes, size - start)
                if len(buffer) < expected:
                    break
                data = bytes(buffer[:expected])
                del buffer[:expected]
                self._store.write_chunk(key, chunk_index, data)
                self._signal(key, chunk_index)
                chunk_index += 1

    def _signal(self, key: str, index: int) -> None:
        event = self._chunk_events.pop((key, index), None)
        if event is not None:
            event.set()

    def _mark_failed(self, key: str, first_chunk: int, last_chunk: int, exc: Exception) -> None:
        for index in range(first_chunk, last_chunk + 1):
            self._failed[f"{key}:{index}"] = exc
            self._signal(key, index)

    async def stream(
        self,
        key: str,
        package: str,
        relpath: str,
        size: int,
        byte_range: ByteRange,
        *,
        prefetch: bool = False,
    ) -> AsyncIterator[bytes]:
        end = min(byte_range.end, size - 1)
        offset = byte_range.start
        while offset <= end:
            index = offset // self._chunk_bytes
            _, chunk_end = self._chunk_bounds(size, index)
            data = await self._chunk(key, package, relpath, size, index)
            within = offset - index * self._chunk_bytes
            take = min(end, chunk_end) - offset + 1
            yield data[within : within + take]
            offset += take
            if prefetch:
                next_index = offset // self._chunk_bytes
                if next_index * self._chunk_bytes < size:
                    self._ensure_window(
                        key, package, relpath, size, self._window_for_chunk(next_index)
                    )

    async def prime(
        self, key: str, package: str, relpath: str, size: int, byte_range: ByteRange
    ) -> None:
        """Ensure the first needed chunk is available (validates upstream)."""
        index = byte_range.start // self._chunk_bytes
        await self._chunk(key, package, relpath, size, index)

    def has_chunk(self, key: str, index: int) -> bool:
        return self._store.has(key, index)

    async def warm(
        self,
        key: str,
        package: str,
        relpath: str,
        size: int,
        length: int,
        *,
        whole_below: int = 0,
    ) -> None:
        """Fetch and cache the head of a clip (whole file when small enough)."""
        if size <= 0:
            return
        target = size if whole_below and size <= whole_below else min(length, size)
        if target <= 0:
            return
        last_chunk = max(0, (target - 1) // self._chunk_bytes)
        last_window = self._window_for_chunk(last_chunk)
        tasks: list[asyncio.Task[object]] = []
        for window in range(last_window + 1):
            self._ensure_window(key, package, relpath, size, window)
            task = self._window_tasks.get((key, window))
            if task is not None:
                tasks.append(task)
        for task in tasks:
            try:
                await asyncio.shield(task)
            except Exception:
                return

    def prefetch_head(
        self,
        key: str,
        package: str,
        relpath: str,
        size: int,
        length: int,
        *,
        whole_below: int = 0,
    ) -> None:
        """Warm the start of an upcoming clip; whole file when small enough."""
        if size <= 0:
            return
        target = size if whole_below and size <= whole_below else min(length, size)
        last_chunk = max(0, (target - 1) // self._chunk_bytes)
        for window in range(0, self._window_for_chunk(last_chunk) + 1):
            self._ensure_window(key, package, relpath, size, window)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def stats(self) -> dict[str, object]:
        data = dict(self._store.stats())
        data["window_bytes"] = self._window_bytes
        data["concurrency"] = self._gate.limit
        data["windows_inflight"] = len(self._window_tasks)
        return data
