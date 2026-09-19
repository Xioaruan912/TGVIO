"""Bounded thumbnail previews for ``/pick``.

Read-only and transport-free: it downloads only embedded thumbnails through the
personal session, tiles them, and hands the image path back to the caller, who
sends it and calls :meth:`release`. It never creates a Job, never touches the
publish pipeline and never keeps media on disk.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import secrets
import shutil
import time
from typing import Awaitable, Callable, Sequence

from tgvio.observability import log_event

ProgressCallback = Callable[[int, int], Awaitable[None]]
ThumbnailFetcher = Callable[[int, int, Path], Awaitable[object | None]]

_STALE_SECONDS = 3600
_MAX_ITEMS = 10


@dataclass(frozen=True, slots=True)
class PickPreview:
    token: str
    image: Path | None
    slots: tuple[bool, ...]
    fetched: int
    total: int

    @property
    def partial(self) -> bool:
        return self.fetched < self.total


class PickPreviewService:
    def __init__(
        self,
        fetch_thumbnail: ThumbnailFetcher,
        grid_builder,
        *,
        cache_root: Path,
        frame_extractor=None,
        max_items: int = _MAX_ITEMS,
        concurrency: int = 2,
        timeout_seconds: float = 45.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._fetch = fetch_thumbnail
        self._grid = grid_builder
        self._frames = frame_extractor
        self._root = Path(cache_root)
        self._max_items = max(1, min(int(max_items), _MAX_ITEMS))
        self._concurrency = max(1, int(concurrency))
        self._timeout = max(5.0, float(timeout_seconds))
        self._now = now or time.time
        self._log = logging.getLogger("tgvio.telegram.preview")

    # ------------------------------------------------------------- lifecycle
    def sweep(self) -> int:
        """Best-effort removal of preview directories left by a crash."""

        removed = 0
        try:
            entries = list(self._root.iterdir())
        except OSError:
            return 0
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
        return removed

    def _safe_dir(self, token: str) -> Path | None:
        value = str(token or "")
        if not value or not all(c.isalnum() or c in "-_" for c in value):
            return None
        candidate = self._root / f"pickpreview-{value}"
        try:
            root = self._root.resolve()
            resolved = candidate.resolve()
        except OSError:
            return None
        if resolved == root or root not in resolved.parents:
            return None
        return candidate

    def release(self, token: str) -> None:
        path = self._safe_dir(token)
        if path is None:
            return
        shutil.rmtree(path, ignore_errors=True)

    def grid_shape(self, count: int) -> tuple[int, int]:
        """Columns/rows of the contact sheet, for the caption hint."""

        builder = getattr(self._grid, "grid_shape", None)
        if callable(builder):
            return builder(int(count))
        return (0, 0)

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

    # ---------------------------------------------------------------- builds
    async def build_page(
        self,
        source_index: int,
        message_ids: Sequence[int],
        *,
        progress: ProgressCallback | None = None,
    ) -> PickPreview:
        self._sweep_once()
        slots = [int(message_id) for message_id in message_ids][: self._max_items]
        return await self._build(source_index, slots, progress=progress)

    async def build_single(
        self,
        source_index: int,
        message_id: int,
        *,
        progress: ProgressCallback | None = None,
    ) -> PickPreview:
        self._sweep_once()
        return await self._build(
            source_index, [int(message_id)], progress=progress
        )

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

    async def _build(
        self,
        source_index: int,
        message_ids: Sequence[int],
        *,
        progress: ProgressCallback | None,
    ) -> PickPreview:
        token = secrets.token_urlsafe(9)
        directory = self._safe_dir(token)
        total = len(message_ids)
        if directory is None or total == 0:
            return PickPreview(token=token, image=None, slots=(), fetched=0, total=total)
        directory.mkdir(parents=True, exist_ok=True)
        fetched = 0
        images: list[Path | None] = [None] * total
        lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(self._concurrency)
        deadline = self._now() + self._timeout

        async def one(position: int, message_id: int) -> None:
            nonlocal fetched
            async with semaphore:
                if self._now() >= deadline:
                    return
                candidate = await self._fetch(int(source_index), int(message_id), directory)
                path = await self._resolve_image(candidate, directory, int(message_id))
                if path is None:
                    return
                images[position] = path
                async with lock:
                    fetched += 1
                    done = fetched
            if progress is not None:
                try:
                    await progress(done, total)
                except Exception:  # noqa: BLE001 - progress is cosmetic
                    pass

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(one(position, mid) for position, mid in enumerate(message_ids))
                ),
                timeout=self._timeout + 5.0,
            )
        except (asyncio.TimeoutError, TimeoutError):
            log_event(
                self._log,
                logging.INFO,
                "telegram.preview.page_timeout",
                "Thumbnail page preview hit its deadline",
                fetched=len([image for image in images if image is not None]),
                total=total,
            )
        except Exception as exc:  # noqa: BLE001 - previews are best effort
            log_event(
                self._log,
                logging.WARNING,
                "telegram.preview.page_failed",
                "Thumbnail page preview failed",
                exception_type=type(exc).__name__,
            )
        if not any(image is not None for image in images):
            log_event(
                self._log,
                logging.INFO,
                "telegram.preview.empty",
                "No preview image could be resolved for this page",
                total=total,
            )
            self.release(token)
            return PickPreview(
                token=token, image=None, slots=tuple(False for _ in images),
                fetched=0, total=total,
            )
        try:
            grid = await self._grid.build(images, directory / "grid.jpg")
        except Exception as exc:  # noqa: BLE001
            log_event(
                self._log,
                logging.WARNING,
                "telegram.preview.grid_error",
                "Thumbnail grid build raised",
                exception_type=type(exc).__name__,
            )
            grid = None
        if grid is None:
            self.release(token)
            return PickPreview(
                token=token, image=None,
                slots=tuple(image is not None for image in images),
                fetched=fetched, total=total,
            )
        return PickPreview(
            token=token,
            image=grid,
            slots=tuple(image is not None for image in images),
            fetched=fetched,
            total=total,
        )
