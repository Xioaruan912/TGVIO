"""Duration and size of plain video files, read from the MP4 header.

Archive packages carry these in their manifest; plain files do not, and without a
duration a file cannot be sorted into the short feed or the long list. Two range
reads at most per file: the head, and the trailing ``moov`` when it sits at the end.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

from tgvio_player.domain.ranges import ByteRange

_LOG = logging.getLogger(__name__)
_HEAD_BYTES = 256 * 1024
_MAX_MOOV_BYTES = 32 * 1024 * 1024
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
_PROBED_TYPES = {"video/mp4", "video/quicktime"}

ReadRange = Callable[[int, int], Awaitable[bytes]]


def _boxes(data: bytes, start: int, end: int):
    position = start
    while position + 8 <= end:
        size = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        header = 8
        if size == 1:
            if position + 16 > end:
                return
            size = int.from_bytes(data[position + 8:position + 16], "big")
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            return
        yield kind, position + header, position + size
        position += size


def parse_moov(moov: bytes) -> tuple[float | None, int | None, int | None]:
    """(duration seconds, width, height) from a complete ``moov`` box body."""
    duration = width = height = None

    def walk(start: int, end: int) -> None:
        nonlocal duration, width, height
        for kind, body, box_end in _boxes(moov, start, end):
            if kind == b"mvhd" and box_end - body >= 20:
                version = moov[body]
                if version == 1 and box_end - body >= 32:
                    scale = int.from_bytes(moov[body + 20:body + 24], "big")
                    length = int.from_bytes(moov[body + 24:body + 32], "big")
                else:
                    scale = int.from_bytes(moov[body + 12:body + 16], "big")
                    length = int.from_bytes(moov[body + 16:body + 20], "big")
                if scale and length and length != 0xFFFFFFFF:
                    duration = round(length / scale, 3)
            elif kind == b"tkhd" and box_end - body >= 84 and width is None:
                w = int.from_bytes(moov[box_end - 8:box_end - 4], "big") >> 16
                h = int.from_bytes(moov[box_end - 4:box_end], "big") >> 16
                if w and h:
                    width, height = w, h
            elif kind in _CONTAINERS:
                walk(body, box_end)

    walk(0, len(moov))
    return duration, width, height


async def probe_mp4(size: int, read: ReadRange) -> tuple[float | None, int | None, int | None] | None:
    head = await read(0, min(_HEAD_BYTES, size) - 1)
    last_end = 0
    for kind, body, box_end in _boxes(head, 0, len(head)):
        if kind == b"moov":
            return parse_moov(head[body:box_end])
        last_end = box_end
    # The walk stops at the first box that runs past the head (mdat); moov follows it.
    position = last_end
    while position < len(head):
        size_field = int.from_bytes(head[position:position + 4], "big")
        if size_field == 1:
            size_field = int.from_bytes(head[position + 8:position + 16], "big")
        if size_field < 8:
            return None
        position += size_field
        if position >= len(head):
            break
    if position + 8 > size or size - position > _MAX_MOOV_BYTES:
        return None
    tail = await read(position, size - 1)
    for kind, body, box_end in _boxes(tail, 0, len(tail)):
        if kind == b"moov":
            return parse_moov(tail[body:box_end])
    return None


class MediaProbeBackfill:
    """Fill in duration and size for videos that arrived without them."""

    def __init__(self, repository, reader, *, should_pause: Callable[[], bool], pause_seconds: float = 2.0) -> None:
        self._repository = repository
        self._reader = reader
        self._should_pause = should_pause
        self._pause_seconds = pause_seconds
        self._failed: set[str] = set()

    async def _read(self, location: tuple[str, str, str | None], start: int, end: int) -> bytes:
        response = await self._reader.open_range(location[0], location[1], ByteRange(start, end))
        try:
            if response.status not in {200, 206}:
                raise OSError(f"upstream status {response.status}")
            want = end - start + 1
            data = bytearray()
            async for chunk in response.body:
                data.extend(chunk)
                if len(data) >= want:
                    break
            return bytes(data[:want])
        finally:
            close = getattr(response.body, "aclose", None)
            if close is not None:
                await close()

    async def probe_one(self, media_id: str) -> bool:
        details = await self._repository.active_media_details(media_id)
        location = await self._repository.active_media_location(media_id)
        if details is None or location is None or str(details.get("mime_type")) not in _PROBED_TYPES:
            return False
        size = int(details["size_bytes"] or 0)
        if size < 16:
            return False
        result = await probe_mp4(size, lambda start, end: self._read(location, start, end))
        if result is None or result[0] is None:
            return False
        await self._repository.save_media_probe(media_id, *result)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            pending = [media_id for media_id in await self._repository.list_unprobed_video_ids(limit=50)
                       if media_id not in self._failed]
            for media_id in pending:
                while self._should_pause() and not stop.is_set():
                    await asyncio.sleep(1)
                if stop.is_set():
                    return
                try:
                    if not await self.probe_one(media_id):
                        self._failed.add(media_id)
                except Exception as exc:
                    self._failed.add(media_id)
                    _LOG.warning("player.probe.failed media=%s error=%s", media_id[:12], type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._pause_seconds)
                except asyncio.TimeoutError:
                    pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=60 if not pending else 1)
            except asyncio.TimeoutError:
                pass
