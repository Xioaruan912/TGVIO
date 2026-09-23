from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

_LOG = logging.getLogger("tgvio_player.warm")


class MediaWarmBackfill:
    """Warm the head of every active video into the disk range cache.

    Each clip caches ``min(size, head_bytes)`` (small clips are cached whole), so
    any clip plays from local disk on first view. Work is bounded by a small
    worker pool that shares the range cache's own concurrency limiter, skips
    clips that already have their first chunk, and pauses only when playback has
    saturated every stream slot.
    """

    def __init__(
        self,
        cache: object,
        repository: object,
        *,
        head_bytes: int,
        workers: int = 4,
        should_pause: Callable[[], bool] | None = None,
        pause_seconds: float = 2.0,
        list_limit: int = 100_000,
        progress_every: int = 50,
    ) -> None:
        self._cache = cache
        self._repository = repository
        self._head_bytes = max(1, int(head_bytes))
        self._workers = max(1, int(workers))
        self._should_pause = should_pause or (lambda: False)
        self._pause_seconds = max(0.1, pause_seconds)
        self._list_limit = max(1, int(list_limit))
        self._progress_every = max(1, progress_every)
        self._completed = 0

    async def _sleep_stop(self, stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if await self._repository.count_active_videos() > 0:
                    break
            except Exception:
                pass
            await self._sleep_stop(stop, 5.0)
        if stop.is_set():
            return
        try:
            media_ids = await self._repository.list_video_ids(limit=self._list_limit)
        except Exception:
            _LOG.warning("player.warm.list_failed", exc_info=True)
            return
        total = len(media_ids)
        _LOG.info("player.warm.started total=%s head_bytes=%s", total, self._head_bytes)
        queue: asyncio.Queue[str] = asyncio.Queue()
        for media_id in media_ids:
            queue.put_nowait(media_id)

        async def worker() -> None:
            while not stop.is_set():
                try:
                    media_id = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if self._cache.has_chunk(media_id, 0):
                    continue
                while not stop.is_set() and self._should_pause():
                    await self._sleep_stop(stop, self._pause_seconds)
                if stop.is_set():
                    return
                try:
                    details = await self._repository.active_media_details(media_id)
                    location = await self._repository.active_media_location(media_id)
                    if details is None or location is None:
                        continue
                    size = int(details.get("size_bytes") or 0)
                    if size <= 0:
                        continue
                    await self._cache.warm(
                        media_id,
                        location[0],
                        location[1],
                        size,
                        self._head_bytes,
                        whole_below=self._head_bytes,
                    )
                    self._completed += 1
                    if self._completed % self._progress_every == 0:
                        _LOG.info(
                            "player.warm.progress done=%s/%s", self._completed, total
                        )
                except Exception:
                    _LOG.warning(
                        "player.warm.item_failed media=%s", media_id[:12], exc_info=True
                    )

        await asyncio.gather(*[asyncio.create_task(worker()) for _ in range(self._workers)])
        _LOG.info("player.warm.completed done=%s total=%s", self._completed, total)
