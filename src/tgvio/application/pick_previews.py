"""Bounded thumbnail previews for the web console's source browser.

Read-only and transport-free: it downloads only embedded thumbnails through the
personal session, tiles them, and hands the image path back to the caller, who
sends it and calls :meth:`release`. It never creates a Job, never touches the
publish pipeline and never keeps media on disk.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import secrets
import shutil
import time
from typing import Awaitable, Callable

from tgvio.observability import log_event

ThumbnailFetcher = Callable[[int, int, Path], Awaitable[object | None]]

_STALE_SECONDS = 3600
_CACHE_TTL_SECONDS = 48 * 3600


class PickPreviewService:
    def __init__(
        self,
        fetch_thumbnail: ThumbnailFetcher,
        *,
        cache_root: Path,
        frame_extractor=None,
        key_provider=None,
        concurrency: int = 2,
        timeout_seconds: float = 45.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._fetch = fetch_thumbnail
        self._frames = frame_extractor
        self._key_provider = key_provider
        self._root = Path(cache_root)
        self._cache_root = Path(cache_root) / "pickthumb"
        self._cache_ttl = float(_CACHE_TTL_SECONDS)
        self._concurrency = max(1, int(concurrency))
        self._timeout = max(5.0, float(timeout_seconds))
        self._now = now or time.time
        self._log = logging.getLogger("tgvio.telegram.preview")

    # ------------------------------------------------------------- lifecycle
    def sweep(self) -> int:
        """Best-effort removal of preview directories and expired image cache."""

        removed = 0
        try:
            entries = list(self._root.iterdir())
        except OSError:
            entries = []
        cutoff = self._now() - _STALE_SECONDS
        for entry in entries:
            if not entry.name.startswith("pickpreview-"):
                continue
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if entry.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            except OSError:
                continue
        removed += self._sweep_cache()
        return removed

    def _sweep_cache(self) -> int:
        """Drop cached thumbnails older than the retention window."""

        if not self._cache_root.is_dir():
            return 0
        cutoff = self._now() - self._cache_ttl
        removed = 0
        try:
            folders = list(self._cache_root.iterdir())
        except OSError:
            return 0
        for folder in folders[:200]:
            try:
                if folder.is_symlink() or not folder.is_dir():
                    continue
                for entry in list(folder.iterdir())[:500]:
                    try:
                        if entry.is_file() and entry.stat().st_mtime < cutoff:
                            entry.unlink(missing_ok=True)
                            removed += 1
                    except OSError:
                        continue
                if not any(folder.iterdir()):
                    folder.rmdir()
            except OSError:
                continue
        return removed

    def _cache_dir(self, source_index: int) -> Path | None:
        if self._key_provider is None:
            return None
        try:
            key = str(self._key_provider(int(source_index)) or "")
        except Exception:  # noqa: BLE001 - a missing key only disables caching
            return None
        if not key or not all(c.isalnum() or c in "-_" for c in key):
            return None
        return self._cache_root / key

    def _cached_image(self, cache_dir: Path | None, message_id: int) -> Path | None:
        if cache_dir is None or not cache_dir.is_dir():
            return None
        try:
            entries = list(cache_dir.glob(f"{int(message_id)}.*"))
        except OSError:
            return None
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                stat = entry.stat()
                if stat.st_size > 0 and (self._now() - stat.st_mtime) < self._cache_ttl:
                    return entry
            except OSError:
                continue
        return None

    def _store_image(self, cache_dir: Path | None, message_id: int, source: Path) -> Path:
        if cache_dir is None:
            return source
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            target = cache_dir / f"{int(message_id)}{source.suffix or '.jpg'}"
            shutil.copyfile(source, target)
            stamp = self._now()
            with suppress(OSError):
                os.utime(target, (stamp, stamp))
            return target
        except OSError:
            return source

    async def _resolve_image(self, candidate, directory: Path, message_id: int) -> Path | None:
        """Turn a fetch result into a local image, extracting a frame if needed."""

        if candidate is None:
            return None
        path = getattr(candidate, "path", candidate)
        if path is None:
            return None
        source = Path(path)
        if not getattr(candidate, "needs_frame", False):
            return source
        if self._frames is None:
            return None
        try:
            return await self._frames.extract(source, directory / f"frame-{message_id}.jpg")
        except Exception as exc:  # noqa: BLE001 - a missing frame is not fatal
            log_event(
                self._log,
                logging.INFO,
                "telegram.preview.frame_failed",
                "Video fragment could not be turned into a frame",
                exception_type=type(exc).__name__,
            )
            return None

    async def thumbnail(self, source_index: int, message_id: int) -> Path | None:
        """One cached preview image for the web console, or None when there is none."""

        self._sweep_once()
        cache_dir = self._cache_dir(source_index)
        cached = self._cached_image(cache_dir, message_id)
        if cached is not None:
            return cached
        self._root.mkdir(parents=True, exist_ok=True)
        directory = self._root / f"pickpreview-{secrets.token_hex(8)}"
        directory.mkdir()
        try:
            candidate = await asyncio.wait_for(
                self._fetch(int(source_index), int(message_id), directory), timeout=self._timeout
            )
            image = await self._resolve_image(candidate, directory, int(message_id))
            if image is None or not image.is_file():
                return None
            stored = self._store_image(cache_dir, message_id, image)
            return stored if stored != image else None
        except (asyncio.TimeoutError, OSError) as exc:
            log_event(self._log, logging.INFO, "telegram.preview.thumbnail_failed",
                      "Single thumbnail could not be fetched", exception_type=type(exc).__name__)
            return None
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    # ---------------------------------------------------------------- builds
    def _sweep_once(self) -> None:
        if getattr(self, "_swept", False):
            return
        self._swept = True
        removed = self.sweep()
        if removed:
            log_event(
                self._log,
                logging.INFO,
                "telegram.preview.swept",
                "Removed stale pick preview directories",
                removed=removed,
            )
