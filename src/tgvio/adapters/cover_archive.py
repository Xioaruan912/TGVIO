"""The maintenance writer uses real HTTP Range and verified bounded JPEG writes."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import threading
import time

from tgvio.adapters.rendition_archive import RenditionArchivePort
from tgvio.adapters.rendition_discovery import RenditionDiscovery
from tgvio.domain.renditions import safe_path
from tgvio.infrastructure.cover_frames import sample
from tgvio.infrastructure.archive_retry import retry_archive


# Compatibility name; discovery has one shared retry implementation.
CoverDiscovery = RenditionDiscovery


class CoverArchivePort(RenditionArchivePort):
    async def read_range(self, path: str, start: int, end: int, size: int) -> bytes:
        return await retry_archive(lambda: self._read_range(path, start, end, size))


    async def _read_range(self, path: str, start: int, end: int, size: int) -> bytes:
        safe_path(path)
        if not 0 <= start <= end < size or end - start + 1 > 8 * 1024**2:
            raise ValueError("cover range exceeds budget")
        stop = threading.Event()
        future = asyncio.create_task(asyncio.to_thread(self._range, path, start, end, size, stop))
        try:
            return await asyncio.shield(future)
        except BaseException:
            stop.set()
            await asyncio.gather(future, return_exceptions=True)
            raise

    def _range(self, path, start, end, size, stop):
        conn = self._connect()
        wanted = end - start + 1
        began = time.monotonic()
        result = bytearray()
        try:
            conn.request("GET", self._quote_path(self._absolute_path(path)),
                         headers={"Authorization": self._authorization,
                                  "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status == 429 or response.status >= 500:
                raise RuntimeError("archive range temporarily unavailable")
            valid = (response.status == 206
                     and response.getheader("Content-Range") == f"bytes {start}-{end}/{size}")
            whole_small = response.status == 200 and start == 0 and wanted == size
            if not (valid or whole_small):
                raise ValueError("archive refused bounded cover range")
            if response.getheader("Content-Length") not in (None, str(wanted)):
                raise ValueError("cover range length mismatch")
            while len(result) < wanted and not stop.is_set():
                if time.monotonic() - began > 90:
                    raise TimeoutError("cover range deadline")
                block = response.read(min(64*1024, wanted - len(result)))
                if not block:
                    break
                result.extend(block)
                # Independent worker: at most 0.5 MiB/s, one source at a time.
                stop.wait(max(0, len(result)/(512*1024) - (time.monotonic() - began)))
            if stop.is_set() or len(result) != wanted:
                raise ValueError("cover range incomplete or cancelled")
            return bytes(result)
        finally:
            conn.close()

    async def sample(self, path: str, size: int, work: Path) -> bytes | None:
        safe_path(path)
        return await asyncio.wait_for(sample(self, path, size, work), 180)

    async def write_cover(self, path: str, payload: bytes) -> None:
        safe_path(path)
        if not 0 < len(payload) <= 1_000_000:
            raise ValueError("cover exceeds byte budget")
        await self.ensure_collection(str(Path(path).parent))
        await self.put_bytes(payload, path, content_type="image/jpeg")
        if not await self.exists(path, len(payload)):
            raise ValueError("cover upload size verification failed")
        actual = await self.get_bytes(path, max_bytes=1_000_000)
        if actual is None or hashlib.sha256(actual).digest() != hashlib.sha256(payload).digest():
            raise ValueError("cover upload hash verification failed")
