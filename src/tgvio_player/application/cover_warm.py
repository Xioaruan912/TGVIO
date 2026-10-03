"""The cover mirror warms itself, inside the bounds the spec fixes.

A wall of thumbnails is only fast if the covers are already local, and nobody is going to run
a command to make that true. So the Player does it: one batch per round, at most half the
cover lanes, one cover at a time with a pause between them, stopping when the disk budget is
full. Progress is "which files are missing", which is why a restart resumes exactly where it
stopped without any checkpoint of its own.

A source the archive says is gone is remembered for the life of the process. The archive
names covers by the hash of their own bytes, so that memory can never hide a cover that
comes back: a re-published cover is a different key.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

from tgvio_player.domain.ranges import ByteRange

_LOG = logging.getLogger("tgvio_player.cover_mirror")

# A backlog is chased at the configured cadence; an idle mirror waits this long for a change.
CATCH_UP_SECONDS = 30.0
IDLE_SECONDS = 900.0
_PACE_SECONDS = 0.2
_RETRY_DELAYS = (1.0, 4.0)


@dataclass
class CoverWarmRun:
    fetched: int = 0
    skipped: int = 0
    failed: int = 0


class CoverWarm:
    def __init__(
        self,
        repository: object,
        reader: object,
        mirror: object,
        counters: object,
        *,
        batch: int = 64,
        concurrency: int = 2,
        catch_up_seconds: float = CATCH_UP_SECONDS,
        pace_seconds: float = _PACE_SECONDS,
        retry_delays: tuple[float, ...] = _RETRY_DELAYS,
        sleep: Callable[[asyncio.Event, float], Awaitable[None]] | None = None,
        pace: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._mirror = mirror
        self._counters = counters
        self._batch = max(1, int(batch))
        self._concurrency = max(1, int(concurrency))
        self._catch_up_seconds = max(1.0, float(catch_up_seconds))
        self._pace_seconds = max(0.0, float(pace_seconds))
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep or self._sleep_until_stop
        self._pace = pace or asyncio.sleep
        # A source the archive called gone. Keyed by content, so a re-published cover with a
        # different hash is never mistaken for the one that vanished.
        self._gone: set[str] = set()

    async def _sleep_until_stop(self, stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            # A failed round is a round, not the end of the loop: a locked database or an
            # unwritable directory must not silently stop warming until the next restart.
            try:
                await self.run_once()
            except Exception:
                _LOG.warning("cover mirror warm round failed", exc_info=True)
            if stop.is_set():
                return
            await self._sleep(
                stop,
                self._catch_up_seconds if self._counters.warm_pending else IDLE_SECONDS,
            )

    async def run_once(self) -> CoverWarmRun:
        rows = await self._repository.mirror_candidates()
        keyed = [(row, self._mirror.key_for(row[2])) for row in rows]
        active = {key for _, key in keyed if key is not None}
        pending = [
            (row, key) for row, key in keyed
            if key is not None
            and key not in self._gone
            and not await asyncio.to_thread(self._mirror.has, key, row[3])
        ]
        self._counters.warm_pending = len(pending)
        result = CoverWarmRun(skipped=len(rows) - len(pending))
        if not pending:
            await asyncio.to_thread(self._mirror.sweep, active)
            return result
        lanes = asyncio.Semaphore(self._concurrency)
        budget = int(getattr(self._mirror, "budget_bytes", 0))
        stored = (await asyncio.to_thread(self._mirror.stats))[1]
        paced = False

        async def fetch(row: tuple[str, str, str, int], key: str) -> None:
            nonlocal stored, paced
            async with lanes:
                if paced:
                    # One cover at a time, with a pause: warming must never be the reason a
                    # viewer's own cover request waits. The pause also serialises the
                    # budget check, so the budget is a bound rather than a suggestion.
                    await self._pace(self._pace_seconds)
                paced = True
                if budget and stored + row[3] > budget:
                    result.skipped += 1
                    return
                if await self._fetch(row, key):
                    stored += row[3]
                    result.fetched += 1
                else:
                    result.failed += 1

        await asyncio.gather(*(fetch(row, key) for row, key in pending[: self._batch]))
        await asyncio.to_thread(self._mirror.sweep, active)
        return result

    async def _fetch(self, row: tuple[str, str, str, int], key: str) -> bool:
        """One cover, retried on a transient answer and never on a missing source."""
        _media_id, remote_path, remote_relpath, size = row
        attempts = 1 + len(self._retry_delays)
        for attempt in range(attempts):
            try:
                upstream = await self._reader.open_range(
                    remote_path, remote_relpath, ByteRange(0, size - 1)
                )
                try:
                    if upstream.status == 404:
                        # The source is gone: that is an answer, not something to retry. The
                        # key is remembered so later rounds do not ask again.
                        self._gone.add(key)
                        self._counters.warm_failed += 1
                        return False
                    if upstream.status not in {200, 206}:
                        raise OSError(f"cover upstream status {upstream.status}")
                    payload = bytearray()
                    async for chunk in upstream.body:
                        if chunk:
                            payload.extend(chunk)
                finally:
                    close = getattr(upstream.body, "close", None)
                    if close is not None:
                        await close()
                if len(payload) != size:
                    raise ValueError("short cover")
                await asyncio.to_thread(self._mirror.write, key, bytes(payload))
                return True
            except Exception as exc:
                if attempt + 1 >= attempts:
                    self._counters.warm_failed += 1
                    _LOG.debug("cover mirror warm failed: %s", type(exc).__name__)
                    return False
                await asyncio.sleep(max(0.0, self._retry_delays[attempt]))
        return False
