"""Permanent deletion: accepted at once, finished in the background, never dropped.

A viewer's delete is written to a durable queue and the media is hidden in the same
transaction, so the request answers in one local write. Removing the archive files
(cover, renditions, then the original) is slow: every file is a WebDAV call through
OpenList to the cloud drive. One worker does that, one deletion and one file at a
time with a short pause between calls, so it never competes with playback for the
drive's call budget.

A deletion that does not finish is retried with a growing wait (one minute up to
one hour) for as long as it takes; it is never given up. The queue holds one row per
media still being deleted and drops it on completion, and each attempt logs one
outcome line, so a long outage costs at most a couple dozen lines a day per media.
Every step is idempotent (an already missing file counts as deleted, and each removed
copy is tombstoned at once), so a retry or a restart only does what is left.

Before the worker starts, a short undo window lets the viewer take it back.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
import time
from typing import Any, Awaitable, Callable, Protocol

_LOG = logging.getLogger("tgvio_player.media_deletion")
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{64}$")

UNDO_SECONDS = 6
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600
# A pause between two archive calls leaves the drive's call budget to playback.
FILE_PAUSE_SECONDS = 0.5
# The longest the worker sleeps without looking at the queue again.
IDLE_POLL_SECONDS = 60


class ArchiveDeleter(Protocol):
    async def delete_location(self, package_path: str, remote_relpath: str) -> bool: ...


class FavoriteRemover(Protocol):
    async def unfavorite(self, media_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    deleted_copies: int
    failed_copies: int
    deleted_covers: int
    failed_covers: int
    removed: bool


def retry_delay(attempts: int) -> int:
    """Wait before attempt `attempts + 1`: 1, 2, 4 … minutes, at most an hour."""
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1))


class MediaDeletionService:
    def __init__(
        self,
        repository: Any,
        deleter: ArchiveDeleter,
        *,
        on_removed: Callable[[str], Awaitable[None]],
        favorites: FavoriteRemover | None = None,
        events: Callable[..., None] | None = None,
        fingerprint: Callable[[str], str] = lambda value: value[:12],
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        undo_seconds: int = UNDO_SECONDS,
        file_pause_seconds: float = FILE_PAUSE_SECONDS,
    ) -> None:
        self._repository = repository
        self._deleter = deleter
        self._on_removed = on_removed
        self._favorites = favorites
        self._events = events or (lambda *_args, **_kwargs: None)
        self._fingerprint = fingerprint
        self._clock = clock
        self._sleep = sleep
        self._undo_seconds = max(0, int(undo_seconds))
        self._file_pause = max(0.0, float(file_pause_seconds))
        self._wake = asyncio.Event()

    def _now(self) -> int:
        return int(self._clock())

    async def request(self, media_id: str) -> int | None:
        """Queue a deletion; returns seconds left to undo, or None for unknown media."""
        if not _MEDIA_ID_RE.fullmatch(media_id):
            return None
        queued = await self._repository.is_media_deletion_queued(media_id)
        if not queued and await self._repository.active_media_details(media_id) is None:
            return None
        now = self._now()
        undo_until = await self._repository.request_media_deletion(
            media_id, now=now, undo_until=now + self._undo_seconds
        )
        if not queued:
            self._events("media_delete_queued", media=self._fingerprint(media_id))
        self._wake.set()
        return max(0, undo_until - now)

    async def cancel(self, media_id: str) -> bool:
        if not _MEDIA_ID_RE.fullmatch(media_id):
            return False
        restored = await self._repository.cancel_media_deletion(media_id, now=self._now())
        if restored:
            self._events("media_delete_undone", media=self._fingerprint(media_id))
        return restored

    async def status(self) -> dict[str, int]:
        return await self._repository.media_deletion_status()

    async def retry_now(self) -> int:
        count = await self._repository.retry_media_deletions_now(now=self._now())
        self._wake.set()
        return count

    async def run_due(self) -> bool:
        """Work on one due deletion. Returns False when nothing was due."""
        claimed = await self._repository.claim_due_media_deletion(now=self._now())
        if claimed is None:
            return False
        media_id, attempts = claimed
        media = self._fingerprint(media_id)
        try:
            outcome = await self.delete_now(media_id)
            reason = "copies_remaining"
        except Exception as exc:  # any failure is retried, never surfaced or dropped
            outcome = None
            reason = type(exc).__name__
        if outcome is not None and outcome.removed:
            await self._repository.complete_media_deletion(media_id)
            _LOG.info("player.media_delete.completed media=%s attempts=%s", media, attempts + 1)
            return True
        wait = retry_delay(attempts + 1)
        await self._repository.retry_media_deletion(
            media_id, error=reason, next_attempt_at=self._now() + wait
        )
        _LOG.warning(
            "player.media_delete.retry media=%s attempt=%s reason=%s wait_s=%s",
            media, attempts + 1, reason, wait,
        )
        return True

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                worked = await self.run_due()
            except Exception:
                _LOG.exception("player.media_delete.worker_failed")
                worked = False
            if worked:
                timeout = 1.0
            else:
                timeout = float(IDLE_POLL_SECONDS)
                try:
                    due = await self._repository.next_media_deletion_due()
                except Exception:
                    due = None
                if due is not None:
                    timeout = min(timeout, max(1.0, due - self._clock()))
            self._wake.clear()
            stopper = asyncio.ensure_future(stop.wait())
            waker = asyncio.ensure_future(self._wake.wait())
            try:
                await asyncio.wait({stopper, waker}, timeout=timeout,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopper.cancel()
                waker.cancel()

    async def delete_now(self, media_id: str) -> DeletionOutcome:
        """Remove every archive file of one media. Covers and renditions go first; the
        original is removed only when nothing else failed, so a partial failure never
        leaves renditions or a cover without their source."""
        media = self._fingerprint(media_id)
        if self._favorites is not None:
            # A deleted video must not survive as its favorite backup copy.
            await self._favorites.unfavorite(media_id)
        repository = self._repository
        locations = await repository.deletion_location_records(media_id)
        variants = await repository.active_variants(media_id)
        variant_locations = [
            (str(v["variant_media_id"]), location)
            for v in variants
            for location in await repository.deletion_location_records(str(v["variant_media_id"]))
        ]
        cover_locations = await repository.active_cover_records(media_id)
        targets = variant_locations + [(media_id, location) for location in locations]
        self._events("media_delete_started", media=media, copies=len(targets))

        deleted = failed = deleted_covers = failed_covers = 0
        calls = 0

        async def remove(package_path: str, relpath: str) -> tuple[bool, str]:
            nonlocal calls
            if calls and self._file_pause:
                await self._sleep(self._file_pause)
            calls += 1
            try:
                return await self._deleter.delete_location(package_path, relpath), "DeleteReturnedFalse"
            except Exception as exc:
                return False, type(exc).__name__

        for package_id, package_path, relpath in cover_locations:
            succeeded, _ = await remove(package_path, relpath)
            if succeeded:
                await repository.record_deleted_cover(media_id, package_id)
                deleted_covers += 1
            else:
                failed_covers += 1
        for copy_index, (target_id, (package_id, package_path, relpath)) in enumerate(targets, start=1):
            if target_id == media_id and (failed_covers or (failed and variant_locations)):
                failed += 1
                continue
            succeeded, failure_kind = await remove(package_path, relpath)
            if not succeeded:
                failed += 1
                self._events("media_delete_copy_failed", media=media, copy=copy_index, error=failure_kind)
                continue
            await repository.record_deleted_location(target_id, package_id, relpath)
            if target_id != media_id:
                await repository.finalize_media_deletion(target_id)
                await self._on_removed(target_id)
            deleted += 1

        removed = await repository.finalize_media_deletion(media_id)
        self._events(
            "media_delete_repository_finalized", media=media,
            deleted_copies=deleted, failed_copies=failed, removed=removed,
        )
        if removed:
            await self._on_removed(media_id)
        return DeletionOutcome(deleted, failed, deleted_covers, failed_covers, removed)
