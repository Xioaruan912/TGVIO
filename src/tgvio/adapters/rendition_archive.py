"""Archive maintenance transport: bounded reads, streaming downloads and verified writes."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import threading
import time

from tgvio.adapters.webdav_archive import WebDavArchiveTransport
from tgvio.application.rendition_backfill import canonical
from tgvio.infrastructure.rendition_encoder import encode
from tgvio.adapters.rendition_recovery import recover
from tgvio.infrastructure.archive_retry import retry_archive


class RenditionArchivePort(WebDavArchiveTransport):
    async def read_json(self, path: str):
        async def read():
            data = await asyncio.to_thread(self._metadata, path)
            return json.loads(data) if data is not None else None
        return await retry_archive(read)

    def _metadata(self, path: str) -> bytes | None:
        conn = self._connect()
        try:
            conn.request("GET", self._quote_path(self._absolute_path(path)),
                         headers={"Authorization": self._authorization})
            response = conn.getresponse()
            if response.status == 404:
                return None
            if response.status != 200:
                raise RuntimeError("archive metadata unavailable")
            data = response.read(512 * 1024 + 1)
            if len(data) > 512 * 1024:
                raise ValueError("archive metadata exceeds budget")
            return data
        finally:
            conn.close()

    async def download(self, path: str, target: Path, size: int, digest: str) -> None:
        if not 0 < size <= 4 * 1024**3:
            raise ValueError("source exceeds maintenance budget")
        await self._transfer(self._download, path, target, size, digest)

    async def _transfer(self, operation, *args):
        stop = threading.Event()
        future = asyncio.create_task(asyncio.to_thread(operation, *args, stop))
        try:
            return await asyncio.shield(future)
        except BaseException:
            stop.set()
            await asyncio.gather(future, return_exceptions=True)
            raise

    async def read_candidate(self, path: str, target: Path, size: int, deadline: float) -> str:
        # Unknown digest is intentional only for bounded, unindexed candidates.
        # The recovery verifier must bind the complete returned hash and media.
        if not 0 < size <= 64 * 1024**2:
            raise ValueError("recovery candidate exceeds budget")
        return await self._transfer(self._read_file, path, target, size, deadline)

    async def recover(self, task, heights, work, max_bytes):
        return await recover(self, task, heights, work, max_bytes)

    def _download(self, path: str, target: Path, size: int, digest: str, stop: threading.Event):
        actual = self._read_file(path, target, size, time.monotonic() + 3600, stop)
        if actual != digest:
            raise ValueError("archive source integrity mismatch")

    def _read_file(self, path: str, target: Path, size: int, deadline: float,
                   stop: threading.Event) -> str:
        conn = self._connect()
        count = 0
        h = hashlib.sha256()
        start = time.monotonic()
        try:
            conn.request("GET", self._quote_path(self._absolute_path(path)),
                         headers={"Authorization": self._authorization})
            response = conn.getresponse()
            if response.status != 200:
                raise RuntimeError("archive source unavailable")
            declared = response.getheader("Content-Length")
            if declared is not None and int(declared) != size:
                raise ValueError("archive source size mismatch")
            with target.open("wb") as f:
                while not stop.is_set():
                    if time.monotonic() > deadline:
                        raise TimeoutError("archive download deadline")
                    block = response.read(min(1024 * 1024, size - count + 1))
                    if not block:
                        break
                    count += len(block)
                    if count > size:
                        raise ValueError("archive download exceeds expected size")
                    f.write(block)
                    h.update(block)
                    # One maintenance transfer, capped at 4 MiB/s.
                    delay = count / (4 * 1024**2) - (time.monotonic() - start)
                    if delay > 0:
                        stop.wait(delay)
            if stop.is_set():
                raise RuntimeError("archive download cancelled")
            if count != size:
                raise ValueError("archive source integrity mismatch")
            return h.hexdigest()
        finally:
            conn.close()

    async def encode(self, source: Path, target: Path, height: int) -> dict:
        return await encode(source, target, height)

    async def upload(self, source: Path, path: str) -> None:
        await self.put_file(source, path, source.stat().st_size)

    async def exists(self, path: str, size: int) -> bool:
        stat = await self.stat(path)
        return bool(stat.exists and stat.size_bytes == size)

    async def write_json(self, path: str, value) -> None:
        await self.put_bytes(canonical(value), path, content_type="application/json")
        if await self.read_json(path) != value:
            raise ValueError("rendition index verification failed")
