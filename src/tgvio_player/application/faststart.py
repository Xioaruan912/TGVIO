from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

_LOG = logging.getLogger("tgvio_player.faststart")

_MP4_MIME = frozenset(
    {"video/mp4", "video/quicktime", "video/x-m4v", "audio/mp4", "audio/x-m4a"}
)
_CONTAINER_BOXES = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex"})
_MAX_TOP_LEVEL_BOXES = 32

ReadRange = Callable[[int, int], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class FaststartOverlay:
    """A virtual faststart view of a progressive MP4 whose ``moov`` is at the end.

    The virtual file contains exactly the original bytes in a different order:
    ``[prefix][moov][everything after prefix except moov]``. Only the absolute
    chunk offsets inside ``moov`` change, so no media data is copied.
    """

    prefix_len: int
    moov: bytes
    size: int
    mime: str

    @property
    def moov_len(self) -> int:
        return len(self.moov)

    def segments(self, start: int, end: int) -> list[tuple[str, int, int]]:
        """Split a virtual range into ``("cache"|"origin", source_offset, length)``."""
        segments: list[tuple[str, int, int]] = []
        cursor = start
        if cursor <= end and cursor < self.prefix_len:
            length = min(end, self.prefix_len - 1) - cursor + 1
            segments.append(("origin", cursor, length))
            cursor += length
        moov_start = self.prefix_len
        if cursor <= end and cursor < moov_start + self.moov_len:
            length = min(end, moov_start + self.moov_len - 1) - cursor + 1
            segments.append(("cache", cursor - moov_start, length))
            cursor += length
        if cursor <= end:
            segments.append(("origin", cursor - self.moov_len, end - cursor + 1))
        return segments

    def original_offset(self, virtual_offset: int) -> int:
        """Map a virtual byte offset back to the original remote offset."""
        if virtual_offset < self.prefix_len:
            return virtual_offset
        if virtual_offset < self.prefix_len + self.moov_len:
            return -1  # lives in the cached moov, not in the remote file
        return virtual_offset - self.moov_len


def is_mp4(details: dict[str, object]) -> bool:
    mime = details.get("mime_type")
    return isinstance(mime, str) and mime.lower() in _MP4_MIME


def _patch_chunk_offsets(
    moov: bytearray,
    delta: int,
    lower_bound: int,
    upper_bound: int,
) -> int | None:
    patched = 0

    def walk(start: int, end: int) -> bool:
        nonlocal patched
        position = start
        while position + 8 <= end:
            size = int.from_bytes(moov[position : position + 4], "big")
            box_type = bytes(moov[position + 4 : position + 8])
            header = 8
            if size == 1:
                if position + 16 > end:
                    return False
                size = int.from_bytes(moov[position + 8 : position + 16], "big")
                header = 16
            if size < header or position + size > end:
                return False
            if box_type == b"stco":
                count = int.from_bytes(moov[position + header + 4 : position + header + 8], "big")
                entries = position + header + 8
                if entries + count * 4 > end:
                    return False
                for index in range(count):
                    offset = int.from_bytes(moov[entries + index * 4 : entries + index * 4 + 4], "big")
                    if not lower_bound <= offset < upper_bound:
                        return False
                    new_offset = offset + delta
                    if new_offset > 0xFFFFFFFF:
                        return False
                    moov[entries + index * 4 : entries + index * 4 + 4] = new_offset.to_bytes(4, "big")
                patched += 1
            elif box_type == b"co64":
                count = int.from_bytes(moov[position + header + 4 : position + header + 8], "big")
                entries = position + header + 8
                if entries + count * 8 > end:
                    return False
                for index in range(count):
                    offset = int.from_bytes(moov[entries + index * 8 : entries + index * 8 + 8], "big")
                    if not lower_bound <= offset < upper_bound:
                        return False
                    moov[entries + index * 8 : entries + index * 8 + 8] = (
                        offset + delta
                    ).to_bytes(8, "big")
                patched += 1
            elif box_type in _CONTAINER_BOXES:
                if not walk(position + header, position + size):
                    return False
            position += size
        return True

    return patched if walk(0, len(moov)) else None


async def build_overlay(size: int, mime: str, read: ReadRange) -> FaststartOverlay | None:
    """Build a virtual faststart overlay, or ``None`` when it is unnecessary/unsafe."""
    if size <= 0 or mime.lower() not in _MP4_MIME:
        return None
    boxes: list[tuple[int, bytes, int, int]] = []
    position = 0
    moov_box: tuple[int, bytes, int, int] | None = None
    for _ in range(_MAX_TOP_LEVEL_BOXES):
        if position + 8 > size:
            break
        raw = await read(position, min(position + 15, size - 1))
        if len(raw) < 8:
            return None
        box_size = int.from_bytes(raw[0:4], "big")
        box_type = raw[4:8]
        header = 8
        if box_size == 1:
            if len(raw) < 16:
                return None
            box_size = int.from_bytes(raw[8:16], "big")
            header = 16
        elif box_size == 0:
            box_size = size - position
        if box_size < header or position + box_size > size:
            return None
        boxes.append((position, box_type, box_size, header))
        if box_type == b"moov":
            moov_box = boxes[-1]
            break
        position += box_size
        if position >= size:
            break

    if moov_box is None:
        return None
    mdats = [box for box in boxes if box[1] == b"mdat"]
    if not mdats:
        return None
    moov_start, _, moov_size, _ = moov_box
    first_mdat = min(box[0] for box in mdats)
    if moov_start < first_mdat:
        return None  # already faststart
    if moov_start + moov_size != size:
        return None  # unsupported trailing boxes after moov
    if moov_size < 16 or moov_size > size:
        return None

    moov_bytes = await read(moov_start, size - 1)
    if len(moov_bytes) != moov_size:
        return None
    patched = bytearray(moov_bytes)
    tracks = _patch_chunk_offsets(patched, moov_size, first_mdat, moov_start)
    if tracks is None or tracks == 0:
        return None
    _LOG.info(
        "player.faststart.overlay_built size=%s prefix=%s moov=%s tracks=%s",
        size,
        first_mdat,
        moov_size,
        tracks,
    )
    return FaststartOverlay(prefix_len=first_mdat, moov=bytes(patched), size=size, mime=mime)


class FaststartService:
    """Build and cache virtual faststart overlays for the Player's MP4 media."""

    def __init__(
        self,
        store: object,
        repository: object,
        reader: object,
        *,
        memory_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._store = store
        self._repository = repository
        self._reader = reader
        self._memory_bytes = max(1, memory_bytes)
        self._memory: OrderedDict[str, FaststartOverlay] = OrderedDict()
        self._memory_size = 0
        self._missing: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def has_overlay(self, media_id: str) -> bool:
        return media_id in self._memory or media_id in self._missing or self._store.load(media_id) is not None

    def _remember(self, media_id: str, overlay: FaststartOverlay) -> None:
        previous = self._memory.pop(media_id, None)
        if previous is not None:
            self._memory_size -= len(previous.moov)
        self._memory[media_id] = overlay
        self._memory_size += len(overlay.moov)
        while self._memory_size > self._memory_bytes and self._memory:
            _, evicted = self._memory.popitem(last=False)
            self._memory_size -= len(evicted.moov)

    def _cached(self, media_id: str) -> FaststartOverlay | None:
        overlay = self._memory.get(media_id)
        if overlay is None:
            overlay = self._store.load(media_id)
            if overlay is not None:
                self._remember(media_id, overlay)
        return overlay

    async def _read_exact(self, location: tuple[str, str, str | None], start: int, end: int) -> bytes:
        from tgvio_player.domain.ranges import ByteRange

        response = await self._reader.open_range(location[0], location[1], ByteRange(start, end))
        want = end - start + 1
        try:
            if response.status != 206:
                return b""
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.body:
                chunks.append(chunk)
                total += len(chunk)
                if total >= want:
                    break
            return b"".join(chunks)[:want]
        finally:
            close = getattr(response.body, "aclose", None)
            if close is not None:
                await close()

    async def overlay_for(
        self,
        media_id: str,
        details: dict[str, object],
    ) -> FaststartOverlay | None:
        if not is_mp4(details) or media_id in self._missing:
            return None
        overlay = self._cached(media_id)
        if overlay is not None:
            return overlay
        lock = self._locks.setdefault(media_id, asyncio.Lock())
        async with lock:
            overlay = self._cached(media_id)
            if overlay is not None:
                return overlay
            if media_id in self._missing:
                return None
            location = await self._repository.active_media_location(media_id)
            if location is None:
                return None
            size = int(details.get("size_bytes") or 0)
            mime = str(details.get("mime_type") or "video/mp4")
            try:
                overlay = await build_overlay(
                    size,
                    mime,
                    lambda start, end: self._read_exact(location, start, end),
                )
            except Exception:
                _LOG.warning("player.faststart.build_failed media=%s", media_id[:12], exc_info=True)
                overlay = None
            if overlay is None:
                self._missing.add(media_id)
                return None
            self._store.save(media_id, overlay)
            self._remember(media_id, overlay)
            return overlay

    async def prepare(self, media_id: str, details: dict[str, object]) -> bool:
        return await self.overlay_for(media_id, details) is not None


class FaststartBackfill:
    """Low-priority background builder for every active video overlay.

    It yields to playback: whenever the server reports an active playback stream
    the backfill waits, and it never runs more than one build at a time.
    """

    def __init__(
        self,
        service: FaststartService,
        repository: object,
        *,
        should_pause: Callable[[], bool] | None = None,
        pause_seconds: float = 2.0,
        idle_sleep_seconds: float = 0.05,
        progress_every: int = 50,
    ) -> None:
        self._service = service
        self._repository = repository
        self._should_pause = should_pause or (lambda: False)
        self._pause_seconds = max(0.1, pause_seconds)
        self._idle_sleep_seconds = max(0.0, idle_sleep_seconds)
        self._progress_every = max(1, progress_every)

    async def _wait_until_idle(self, stop: asyncio.Event) -> bool:
        while not stop.is_set() and self._should_pause():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._pause_seconds)
            except asyncio.TimeoutError:
                continue
        return not stop.is_set()

    async def run(self, stop: asyncio.Event) -> None:
        # Wait for the first catalog sync so the id list is meaningful.
        while not stop.is_set():
            try:
                if await self._repository.count_active_videos() > 0:
                    break
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
        if stop.is_set():
            return
        try:
            media_ids = await self._repository.list_active_video_ids(limit=100_000)
        except Exception:
            _LOG.warning("player.faststart.backfill.list_failed", exc_info=True)
            return
        total = len(media_ids)
        built = 0
        for index, media_id in enumerate(media_ids, start=1):
            if not await self._wait_until_idle(stop):
                return
            if self._service.has_overlay(media_id):
                continue
            try:
                details = await self._repository.active_media_details(media_id)
                if details is not None and is_mp4(details):
                    if await self._service.overlay_for(media_id, details) is not None:
                        built += 1
            except Exception:
                _LOG.warning("player.faststart.backfill.item_failed media=%s", media_id[:12], exc_info=True)
            if index % self._progress_every == 0:
                _LOG.info("player.faststart.backfill progress=%s/%s built=%s", index, total, built)
            if self._idle_sleep_seconds:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._idle_sleep_seconds)
                except asyncio.TimeoutError:
                    pass
        _LOG.info("player.faststart.backfill completed total=%s built=%s", total, built)
