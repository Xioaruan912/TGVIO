from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import logging
import time

from tgvio_player.domain.ranges import ByteRange

_LOG = logging.getLogger("tgvio_player.rangecache")
_PRIME_BYTES = 64 * 1024


class RangeCacheError(RuntimeError):
    """A remote read failed while filling the cache."""


class _AdaptiveGate:
    """Concurrency limiter that shrinks on throttling (403/429/5xx) and recovers."""

    def __init__(self, limit: int) -> None:
        self.max = max(1, int(limit))
        self.limit = self.max
        self.active = 0
        self.waiting_playback = 0
        self._condition = asyncio.Condition()

    async def acquire(
        self, *, low_priority: bool | Callable[[], bool] = False
    ) -> None:
        async with self._condition:
            playback_waiter = False
            try:
                while True:
                    is_low_priority = (
                        low_priority() if callable(low_priority) else low_priority
                    )
                    if not is_low_priority and not playback_waiter:
                        self.waiting_playback += 1
                        playback_waiter = True
                        self._condition.notify_all()
                    elif is_low_priority and playback_waiter:
                        self.waiting_playback -= 1
                        playback_waiter = False
                        self._condition.notify_all()
                    is_limited = (
                        self.active >= self.limit
                        or (is_low_priority and self.waiting_playback > 0)
                        or (
                            is_low_priority
                            and self.limit > 1
                            and self.active >= self.limit - 1
                        )
                    )
                    if not is_limited:
                        self.active += 1
                        return
                    await self._condition.wait()
            finally:
                if playback_waiter:
                    self.waiting_playback -= 1
                    self._condition.notify_all()

    async def notify_waiters(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    async def release(self) -> None:
        async with self._condition:
            self.active = max(0, self.active - 1)
            self._condition.notify_all()

    async def penalize(self) -> None:
        async with self._condition:
            self.limit = max(1, self.limit - 1)

    async def reward(self) -> None:
        async with self._condition:
            if self.limit < self.max:
                self.limit += 1
                self._condition.notify_all()


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
        self._partial_chunks: dict[tuple[str, int], bytearray] = {}
        self._partial_events: dict[tuple[str, int], asyncio.Event] = {}
        self._chunk_conditions: dict[tuple[str, int], asyncio.Condition] = {}
        self._foreground_windows: dict[tuple[str, int], int] = {}
        self._failed: dict[str, Exception] = {}
        self._tasks: set[asyncio.Task[object]] = set()
        self._disk_cache_bytes_served = 0
        self._inflight_bytes_served = 0
        self._upstream_bytes = 0
        self._window_fetches = 0
        self._window_failures = 0
        self._prime_wait_ms = 0.0
        self._prime_waits = 0

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
            self._disk_cache_bytes_served += len(cached)
            return cached
        event = self._chunk_events.setdefault((key, index), asyncio.Event())
        window = self._window_for_chunk(index)
        self._mark_foreground(key, window)
        try:
            self._ensure_window(key, package, relpath, size, window)
            await event.wait()
            failure = self._failed.get(f"{key}:{index}")
            if failure is not None:
                raise RangeCacheError(str(failure))
            cached = self._store.read_slice(key, index, 0, self._chunk_bytes)
            if cached is None:
                raise RangeCacheError("chunk unavailable after fetch")
            return cached
        finally:
            self._unmark_foreground(key, window)

    def _ensure_window(
        self,
        key: str,
        package: str,
        relpath: str,
        size: int,
        window: int,
        *,
        low_priority: bool = False,
    ) -> None:
        if window * self._window_bytes >= size:
            return
        if (key, window) in self._window_tasks:
            return
        # A failed window belongs to that fetch attempt. Let a later playback
        # request start a fresh fetch instead of inheriting a stale failure.
        window_start = window * self._window_bytes
        window_end = min(window_start + self._window_bytes, size) - 1
        first_chunk = window_start // self._chunk_bytes
        last_chunk = window_end // self._chunk_bytes
        if all(
            self._store.read_slice(key, item, 0, self._chunk_bytes) is not None
            for item in range(first_chunk, last_chunk + 1)
        ):
            return
        for index in range(first_chunk, last_chunk + 1):
            chunk_key = (key, index)
            self._partial_chunks.pop(chunk_key, None)
            self._failed.pop(f"{key}:{index}", None)
        task = asyncio.create_task(
            self._fetch_window(
                key, package, relpath, size, window, low_priority=low_priority
            )
        )
        self._window_tasks[(key, window)] = task
        self._tasks.add(task)

        def _done(_task: asyncio.Task[object]) -> None:
            self._window_tasks.pop((key, window), None)
            self._tasks.discard(_task)

        task.add_done_callback(_done)

    async def _fetch_window(
        self,
        key: str,
        package: str,
        relpath: str,
        size: int,
        window: int,
        *,
        low_priority: bool = False,
    ) -> None:
        window_start = window * self._window_bytes
        window_end = min(window_start + self._window_bytes, size) - 1
        first_chunk = window_start // self._chunk_bytes
        last_chunk = window_end // self._chunk_bytes

        attempt = 0
        while attempt < self._max_attempts:
            while (
                low_priority
                and self._should_pause()
                and not self._window_is_foreground(key, window)
            ):
                await asyncio.sleep(0.2)
            await self._gate.acquire(
                low_priority=lambda: low_priority
                and not self._window_is_foreground(key, window)
            )
            if (
                low_priority
                and self._should_pause()
                and not self._window_is_foreground(key, window)
            ):
                await self._gate.release()
                continue
            response = None
            try:
                response = await self._reader.open_range(
                    package, relpath, ByteRange(window_start, window_end)
                )
                if response.status in {200, 206}:
                    started = time.perf_counter()
                    self._window_fetches += 1
                    await self._consume_window(
                        key,
                        size,
                        response,
                        first_chunk,
                        last_chunk,
                        low_priority=low_priority,
                        window=window,
                    )
                    await self._gate.reward()
                    elapsed = max(0.001, time.perf_counter() - started)
                    _LOG.info(
                        "player.rangecache.window_complete key=%s win=%s bytes=%s elapsed_ms=%.1f mbps=%.2f",
                        key[:12], window, window_end - window_start + 1, elapsed * 1000,
                        (window_end - window_start + 1) / elapsed / 1_000_000,
                    )
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
        self._window_failures += 1
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
        *,
        low_priority: bool = False,
        window: int = 0,
    ) -> None:
        buffer = bytearray()
        chunk_index = first_chunk
        async for piece in response.body:  # type: ignore[attr-defined]
            while (
                low_priority
                and self._should_pause()
                and not self._window_is_foreground(key, window)
            ):
                await asyncio.sleep(0.2)
            if not piece:
                continue
            self._upstream_bytes += len(piece)
            piece_offset = 0
            while piece_offset < len(piece) and chunk_index <= last_chunk:
                start = chunk_index * self._chunk_bytes
                expected = min(self._chunk_bytes, size - start)
                take = min(expected - len(buffer), len(piece) - piece_offset)
                buffer.extend(piece[piece_offset : piece_offset + take])
                piece_offset += take
                partial_key = (key, chunk_index)
                self._partial_chunks[partial_key] = buffer
                self._signal_partial(key, chunk_index)
                if len(buffer) < expected:
                    continue
                data = bytes(buffer)
                self._store.write_chunk(key, chunk_index, data)
                self._failed.pop(f"{key}:{chunk_index}", None)
                self._partial_chunks.pop(partial_key, None)
                self._signal_partial(key, chunk_index)
                self._signal(key, chunk_index)
                chunk_index += 1
                buffer = bytearray()

        if chunk_index <= last_chunk:
            raise RangeCacheError("upstream window ended before all requested chunks arrived")

    def _window_is_foreground(self, key: str, window: int) -> bool:
        return self._foreground_windows.get((key, window), 0) > 0

    def _mark_foreground(self, key: str, window: int) -> None:
        window_key = (key, window)
        self._foreground_windows[window_key] = self._foreground_windows.get(window_key, 0) + 1
        asyncio.create_task(self._gate.notify_waiters())

    def _unmark_foreground(self, key: str, window: int) -> None:
        window_key = (key, window)
        count = self._foreground_windows.get(window_key, 0)
        if count <= 1:
            self._foreground_windows.pop(window_key, None)
        else:
            self._foreground_windows[window_key] = count - 1

    def _signal(self, key: str, index: int) -> None:
        event = self._chunk_events.pop((key, index), None)
        if event is not None:
            event.set()
        self._chunk_conditions.pop((key, index), None)

    def _signal_partial(self, key: str, index: int) -> None:
        event = self._partial_events.pop((key, index), None)
        if event is not None:
            event.set()
        condition = self._chunk_conditions.get((key, index))
        if condition is not None:
            asyncio.create_task(self._notify_chunk_condition(condition))

    @staticmethod
    async def _notify_chunk_condition(condition: asyncio.Condition) -> None:
        async with condition:
            condition.notify_all()

    def _signal_chunk(self, key: str, index: int) -> None:
        event = self._chunk_events.pop((key, index), None)
        if event is not None:
            event.set()

    def _mark_failed(self, key: str, first_chunk: int, last_chunk: int, exc: Exception) -> None:
        for index in range(first_chunk, last_chunk + 1):
            self._failed[f"{key}:{index}"] = exc
            self._signal_partial(key, index)
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
            chunk_start, chunk_end = self._chunk_bounds(size, index)
            within = offset - index * self._chunk_bytes
            take = min(end, chunk_end) - offset + 1
            data = await self._available_chunk_bytes(
                key, package, relpath, size, index, within, take
            )
            yield data
            offset += len(data)
            if prefetch:
                next_index = offset // self._chunk_bytes
                if next_index * self._chunk_bytes < size:
                    self._ensure_window(
                        key,
                        package,
                        relpath,
                        size,
                        self._window_for_chunk(next_index),
                        low_priority=True,
                    )

    async def prime(
        self, key: str, package: str, relpath: str, size: int, byte_range: ByteRange
    ) -> None:
        """Wait for a small first fragment so the HTTP response can start early."""
        started = time.perf_counter()
        index = byte_range.start // self._chunk_bytes
        chunk_start, chunk_end = self._chunk_bounds(size, index)
        within = byte_range.start - chunk_start
        needed = min(
            _PRIME_BYTES,
            byte_range.end - byte_range.start + 1,
            chunk_end - byte_range.start + 1,
        )
        partial_key = (key, index)
        window = self._window_for_chunk(index)
        self._mark_foreground(key, window)
        try:
            self._ensure_window(key, package, relpath, size, window)
            while True:
                cached = self._store.read_slice(key, index, within, needed)
                if cached is not None and len(cached) >= needed:
                    self._disk_cache_bytes_served += len(cached)
                    break
                partial = self._partial_chunks.get(partial_key)
                if partial is not None and len(partial) >= within + needed:
                    break
                # Another request may have caused this chunk to be persisted since
                # the initial lookup; ensure the active fetch generation is reused.
                self._ensure_window(key, package, relpath, size, window)
                failed_key = f"{key}:{index}"
                failure = self._failed.get(failed_key)
                if failure is not None:
                    raise RangeCacheError(str(failure))
                event = self._partial_events.setdefault(partial_key, asyncio.Event())
                event.clear()
                cached = self._store.read_slice(key, index, within, needed)
                partial = self._partial_chunks.get(partial_key)
                if (cached is not None and len(cached) >= needed) or (
                    partial is not None and len(partial) >= within + needed
                ):
                    continue
                await event.wait()
        finally:
            self._unmark_foreground(key, window)
        self._prime_wait_ms += (time.perf_counter() - started) * 1000
        self._prime_waits += 1

    async def _available_chunk_bytes(
        self,
        key: str,
        package: str,
        relpath: str,
        size: int,
        index: int,
        offset: int,
        length: int,
    ) -> bytes:
        partial_key = (key, index)
        window = self._window_for_chunk(index)
        self._mark_foreground(key, window)
        try:
            self._ensure_window(key, package, relpath, size, window)
            while True:
                cached = self._store.read_slice(key, index, offset, length)
                if cached:
                    self._disk_cache_bytes_served += len(cached)
                    return cached
                partial = self._partial_chunks.get(partial_key)
                if partial is not None and len(partial) > offset:
                    data = bytes(partial[offset : min(offset + length, len(partial))])
                    self._inflight_bytes_served += len(data)
                    return data
                # The request may need bytes beyond the current partial prefix.
                # Wait for the next body piece, not for the entire cache chunk.
                failed_key = f"{key}:{index}"
                failure = self._failed.get(failed_key)
                if failure is not None:
                    raise RangeCacheError(str(failure))
                condition = self._chunk_conditions.setdefault(partial_key, asyncio.Condition())
                async with condition:
                    await condition.wait_for(
                        lambda: (
                            (partial := self._partial_chunks.get(partial_key)) is not None
                            and len(partial) > offset
                        )
                        or self._failed.get(f"{key}:{index}") is not None
                        or self._store.read_slice(key, index, offset, length) is not None
                    )
        finally:
            self._unmark_foreground(key, window)

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
            self._ensure_window(key, package, relpath, size, window, low_priority=True)
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
            self._ensure_window(key, package, relpath, size, window, low_priority=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def discard(self, key: str) -> None:
        tasks = [
            task
            for (media_id, _window), task in list(self._window_tasks.items())
            if media_id == key
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for mapping in (
            self._chunk_events,
            self._partial_chunks,
            self._partial_events,
            self._chunk_conditions,
            self._foreground_windows,
        ):
            for item in [item for item in mapping if item[0] == key]:
                mapping.pop(item, None)
        for item in [item for item in self._failed if item.startswith(f"{key}:")]:
            self._failed.pop(item, None)
        self._store.delete_key(key)

    def stats(self) -> dict[str, object]:
        data = dict(self._store.stats())
        data["window_bytes"] = self._window_bytes
        data["concurrency"] = self._gate.limit
        data["windows_inflight"] = len(self._window_tasks)
        data["disk_cache_bytes_served"] = self._disk_cache_bytes_served
        data["inflight_bytes_served"] = self._inflight_bytes_served
        data["upstream_bytes"] = self._upstream_bytes
        data["window_fetches"] = self._window_fetches
        data["window_failures"] = self._window_failures
        data["prime_wait_ms_avg"] = (
            self._prime_wait_ms / self._prime_waits if self._prime_waits else 0.0
        )
        return data
