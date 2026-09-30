"""Sample one bounded cover frame from a remote original without downloading it.

A backfill must not pull a whole video to produce a small still. This adapter
reads only a bounded head range from the archive, decodes a single frame from
that sample with the existing transformer, and deletes everything it created.

A truncated sample still yields the leading frames: sampling passes no duration,
so the transformer falls back to seeking at zero, which decodes from the start of
whatever bytes are present instead of jumping past the end of the sample.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol
import asyncio
import tempfile

# Enough to cover a faststart header plus the first seconds of video; a sample
# is never grown to make a cover - a miss simply leaves that item without one.
DEFAULT_HEAD_BYTES = 8 * 1024 * 1024
DEFAULT_ITEM_TIMEOUT_SECONDS = 30.0
SAMPLE_NAME = "cover-sample.bin"


class RangeReader(Protocol):
    async def read_range(self, remote_path: str, start: int, end: int) -> bytes: ...


class ThumbnailMaker(Protocol):
    async def make_video_thumbnail(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
        duration_seconds: float | None,
    ) -> Path | None: ...


class HeadSampleCoverPort:
    """A ``FrameFromHead`` port: bounded, isolated and never raising."""

    def __init__(
        self,
        reader: RangeReader,
        transformer: ThumbnailMaker,
        *,
        head_bytes: int = DEFAULT_HEAD_BYTES,
        item_timeout_seconds: float = DEFAULT_ITEM_TIMEOUT_SECONDS,
        work_root: Path | None = None,
    ) -> None:
        self._reader = reader
        self._transformer = transformer
        self._head_bytes = max(1, int(head_bytes))
        self._item_timeout_seconds = max(1.0, float(item_timeout_seconds))
        self._work_root = work_root

    async def __call__(self, source_path: str) -> bytes | None:
        try:
            return await asyncio.wait_for(
                self._sample(source_path), timeout=self._item_timeout_seconds
            )
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

    async def _sample(self, source_path: str) -> bytes | None:
        if not self._safe_path(source_path):
            return None
        payload = await self._reader.read_range(source_path, 0, self._head_bytes - 1)
        if not isinstance(payload, bytes) or not payload or len(payload) > self._head_bytes:
            return None
        with tempfile.TemporaryDirectory(dir=self._work_root, prefix="cover-sample-") as tmp:
            root = Path(tmp)
            sample = root / SAMPLE_NAME
            sample.write_bytes(payload)
            frame = await self._transformer.make_video_thumbnail(
                sample, root, item_index=0, duration_seconds=None
            )
            if frame is None or frame.is_symlink() or not frame.is_file():
                return None
            data = frame.read_bytes()
        return data or None

    @staticmethod
    def _safe_path(source_path: str) -> bool:
        if not isinstance(source_path, str) or not source_path.strip():
            return False
        if source_path.startswith("/") or "\\" in source_path:
            return False
        return ".." not in source_path.split("/")
