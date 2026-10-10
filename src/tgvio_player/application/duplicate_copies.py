"""Remove the spare copies of videos the archive stores more than once.

The Bot archives every forward as its own package, so the same video forwarded twice
is stored twice. The Player keeps one copy (the one with the cover or renditions,
otherwise the oldest) and deletes the others, slowly and only while nothing plays:

- the kept copy is first confirmed on the drive, so a spare is never the last copy;
- a spare goes through the same archive delete as a viewer's delete, and is then
  tombstoned so a catalog refresh does not bring it back;
- one file per pause, so the drive's call budget stays with playback.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable, Protocol

_LOG = logging.getLogger("tgvio_player.duplicate_copies")

PAUSE_SECONDS = 20.0
# How often to look again once every spare is gone; new ones only come with new forwards.
IDLE_SECONDS = 3600.0
BATCH = 20


class DirectoryLister(Protocol):
    async def list_collection(self, remote_path: str) -> tuple[Any, ...]: ...


class CopyDeleter(Protocol):
    async def delete_location(self, package_path: str, remote_relpath: str) -> bool: ...


@dataclass(slots=True)
class CleanupResult:
    removed: int = 0
    freed_bytes: int = 0
    skipped: int = 0
    failed: int = 0


class DuplicateCopyCleaner:
    def __init__(
        self,
        repository: Any,
        lister: DirectoryLister,
        deleter: CopyDeleter,
        *,
        should_pause: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pause_seconds: float = PAUSE_SECONDS,
    ) -> None:
        self._repository = repository
        self._lister = lister
        self._deleter = deleter
        self._should_pause = should_pause
        self._sleep = sleep
        self._pause = pause_seconds
        # Spares that failed this run; tried again on the next pass, not in a loop.
        self._failed: set[tuple[str, str, str]] = set()

    async def _exists(self, package_path: str, relpath: str) -> bool:
        path = PurePosixPath(package_path.rstrip("/")) / relpath
        names = {entry.name for entry in await self._lister.list_collection(str(path.parent))}
        return path.name in names

    async def run_once(self, *, limit: int = BATCH) -> CleanupResult:
        result = CleanupResult()
        for copy in await self._repository.redundant_copies(limit=limit + len(self._failed)):
            key = (copy.media_id, copy.extra.package_id, copy.extra.relpath)
            if key in self._failed:
                continue
            if result.removed + result.failed + result.skipped >= limit:
                break
            while self._should_pause():
                await self._sleep(self._pause)
            if not await self._repository.is_redundant_copy(copy):
                result.skipped += 1
                continue
            try:
                if not await self._exists(copy.keep.package_path, copy.keep.relpath):
                    # The catalog thinks the kept copy is there but the drive does not:
                    # leave both to the next catalog refresh rather than guess.
                    result.skipped += 1
                    self._failed.add(key)
                    continue
                deleted = await self._deleter.delete_location(copy.extra.package_path, copy.extra.relpath)
            except Exception as exc:  # retried on the next pass
                _LOG.warning("player.duplicate_copy.failed media=%s error=%s",
                             copy.media_id[:12], type(exc).__name__)
                deleted = False
            if deleted:
                await self._repository.record_deleted_location(
                    copy.media_id, copy.extra.package_id, copy.extra.relpath)
                result.removed += 1
                result.freed_bytes += copy.size_bytes
                _LOG.info("player.duplicate_copy.removed media=%s package=%s bytes=%s",
                          copy.media_id[:12], copy.extra.package_id[:12], copy.size_bytes)
            else:
                result.failed += 1
                self._failed.add(key)
            await self._sleep(self._pause)
        return result

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self.run_once()
            except Exception:
                _LOG.exception("player.duplicate_copy.worker_failed")
                result = CleanupResult()
            if result.removed or result.failed:
                _LOG.info("player.duplicate_copy.batch removed=%s freed_bytes=%s skipped=%s failed=%s",
                          result.removed, result.freed_bytes, result.skipped, result.failed)
            else:
                self._failed.clear()  # nothing left but failures: give them a fresh pass later
            if result.removed:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=IDLE_SECONDS)
            except asyncio.TimeoutError:
                pass
