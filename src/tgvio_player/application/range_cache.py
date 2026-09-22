from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import logging

from tgvio_player.domain.ranges import ByteRange

_LOG = logging.getLogger("tgvio_player.rangecache")


class RangeCacheError(RuntimeError):
    """A transient remote read failed while filling the cache."""


class MediaRangeCache:
    """Streams media byte ranges through a bounded on-disk chunk cache.

    A miss reads exactly the requested chunk from upstream (never the whole
    file), stores it, and serves the requested slice. Once idle it prefetches
    the chunks that follow the playhead so seeking inside already-watched
    regions is served from local disk. Prefetch is strictly lower priority than
    playback and pauses while a playback stream is under pressure.
    """

    def __init__(
        self,
        store: object,
        reader: object,
        *,
        prefetch_chunks: int = 8,
        should_pause: Callable[[], bool] | None = None,
        prefetch_sleep: float = 0.05,
    ) -> None:
        self._store = store
        self._reader = reader
        self._chunk_bytes = int(store.chunk_bytes)
        self._prefetch_chunks = max(0, int(prefetch_chunks))
        self._should_pause = should_pause or (lambda: False)
        self._prefetch_sleep = max(0.0, prefetch_sleep)
        self._inflight: dict[tuple[str, int], asyncio.Future[bytes]] = {}
        self._prefetching: set[str] = set()
        self._tasks: set[asyncio.Task[object]] = set()

    def open(self) -> None:
        self._store.open()

    async def prime(
        self, key: str, package: str, relpath: str, size: int, byte_range: ByteRange
    ) -> None:
        """Ensure the first needed chunk is locally available (validates upstream)."""
        index = byte_range.start // self._chunk_bytes
        await self._read_chunk(key, package, relpath, size, index)

    async def _read_chunk(self, key: str, package: str, relpath: str, size: int, index: int) -> bytes:
        cached = self._store.read_slice(key, index, 0, self._chunk_bytes)
        if cached is not None:
            return cached
        inflight_key = (key, index)
        existing = self._inflight.get(inflight_key)
        if existing is not None:
            return await existing
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        self._inflight[inflight_key] = future
        try:
            data = await self._fetch_chunk(package, relpath, size, index)
            self._store.write_chunk(key, index, data)
            future.set_result(data)
            return data
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(inflight_key, None)

    async def _fetch_chunk(self, package: str, relpath: str, size: int, index: int) -> bytes:
        chunk_start = index * self._chunk_bytes
        if chunk_start >= size:
            raise RangeCacheError("chunk beyond end of media")
        chunk_end = min(chunk_start + self._chunk_bytes, size) - 1
        want = chunk_end - chunk_start + 1
        response = await self._reader.open_range(package, relpath, ByteRange(chunk_start, chunk_end))
        buffer = bytearray()
        try:
            if response.status not in {200, 206}:
                raise RangeCacheError(f"unexpected upstream status {response.status}")
            async for chunk in response.body:
                if chunk:
                    buffer.extend(chunk)
                    if len(buffer) >= want:
                        break
        finally:
            close = getattr(response.body, "aclose", None)
            if close is not None:
                await close()
        if len(buffer) != want:
            raise RangeCacheError("short chunk read")
        return bytes(buffer)

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
            within = offset - index * self._chunk_bytes
            take = min(end - offset + 1, self._chunk_bytes - within)
            data = await self._read_chunk(key, package, relpath, size, index)
            yield data[within : within + take]
            if prefetch and self._prefetch_chunks:
                self._schedule_prefetch(key, package, relpath, size, index + 1)
            offset += take

    def _schedule_prefetch(
        self, key: str, package: str, relpath: str, size: int, start_index: int
    ) -> None:
        if start_index * self._chunk_bytes >= size or key in self._prefetching:
            return
        self._prefetching.add(key)
        task = asyncio.create_task(self._prefetch(key, package, relpath, size, start_index))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _prefetch(
        self, key: str, package: str, relpath: str, size: int, start_index: int
    ) -> None:
        try:
            for step in range(self._prefetch_chunks):
                index = start_index + step
                if index * self._chunk_bytes >= size:
                    return
                while self._should_pause():
                    await asyncio.sleep(0.2)
                try:
                    await self._read_chunk(key, package, relpath, size, index)
                except Exception:
                    _LOG.info("player.rangecache.prefetch_failed key=%s", key[:12])
                    return
                if self._prefetch_sleep:
                    await asyncio.sleep(self._prefetch_sleep)
        finally:
            self._prefetching.discard(key)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def stats(self) -> dict[str, object]:
        data = dict(self._store.stats())
        data["inflight"] = len(self._inflight)
        data["prefetching"] = len(self._prefetching)
        return data
