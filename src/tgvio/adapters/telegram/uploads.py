from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
import time
from typing import Protocol

from telethon import custom, helpers
from telethon.tl import functions, types

from tgvio.observability import log_event


class TelegramUploadClient(Protocol):
    async def __call__(self, request): ...

    async def upload_file(self, file, **kwargs): ...


class BoundedTelegramUploader:
    """Upload local files with bounded concurrent MTProto part requests.

    The upload handle itself is not a visible Telegram side effect. If the
    concurrent part path fails, callers may safely fall back to Telethon's
    normal sequential upload before any message send occurs.
    """

    _BIG_FILE_THRESHOLD = 10 * 1024 * 1024

    def __init__(
        self,
        client: TelegramUploadClient,
        *,
        per_file_workers: int,
        global_workers: int,
        part_size_kb: int,
    ) -> None:
        if not 1 <= int(per_file_workers) <= 32:
            raise ValueError("per_file_workers must be in range 1..32")
        if not 1 <= int(global_workers) <= 32:
            raise ValueError("global_workers must be in range 1..32")
        if int(part_size_kb) not in {64, 128, 256, 512}:
            raise ValueError("part_size_kb must be 64/128/256/512")
        self._client = client
        self._per_file_workers = int(per_file_workers)
        self._global_workers = int(global_workers)
        self._part_size_kb = int(part_size_kb)
        self._part_size = self._part_size_kb * 1024
        self._global_semaphore = asyncio.Semaphore(self._global_workers)
        self._log = logging.getLogger("tgvio.telegram.upload")

    @property
    def per_file_workers(self) -> int:
        return self._per_file_workers

    @property
    def global_workers(self) -> int:
        return self._global_workers

    @property
    def part_size_kb(self) -> int:
        return self._part_size_kb

    async def upload(
        self,
        source: Path,
        *,
        job_id: str | None = None,
        item_index: int | None = None,
    ):
        source = Path(source)
        file_size = source.stat().st_size
        part_count = max(1, (file_size + self._part_size - 1) // self._part_size)
        if file_size <= 0 or self._per_file_workers <= 1 or part_count <= 1:
            return await self._client.upload_file(str(source))

        started_at = time.monotonic()
        workers = min(self._per_file_workers, part_count)
        log_event(
            self._log,
            logging.INFO,
            "telegram.upload.concurrent_started",
            job_id=job_id,
            item_index=item_index,
            file_size_bytes=file_size,
            part_count=part_count,
            per_file_workers=workers,
            global_workers=self._global_workers,
            part_size_kb=self._part_size_kb,
        )
        try:
            uploaded = await self._upload_concurrent(
                source,
                file_size=file_size,
                part_count=part_count,
                workers=workers,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "telegram.upload.concurrent_fallback",
                "Concurrent Telegram upload failed before visible send; falling back to Telethon upload",
                job_id=job_id,
                item_index=item_index,
                file_size_bytes=file_size,
                exception_type=type(exc).__name__,
            )
            return await self._client.upload_file(str(source))

        duration = max(0.000001, time.monotonic() - started_at)
        log_event(
            self._log,
            logging.INFO,
            "telegram.upload.concurrent_completed",
            job_id=job_id,
            item_index=item_index,
            file_size_bytes=file_size,
            part_count=part_count,
            per_file_workers=workers,
            global_workers=self._global_workers,
            duration_ms=int(duration * 1000),
            throughput_mib_s=round(file_size / (1024 * 1024) / duration, 2),
        )
        return uploaded

    async def _upload_concurrent(
        self,
        source: Path,
        *,
        file_size: int,
        part_count: int,
        workers: int,
    ):
        file_id = helpers.generate_random_long()
        file_name = source.name
        is_big = file_size > self._BIG_FILE_THRESHOLD
        hash_md5 = None
        if not is_big:
            hash_md5 = await asyncio.to_thread(self._md5_file, source)

        async def send_part(part_index: int) -> None:
            offset = part_index * self._part_size
            expected = min(self._part_size, max(0, file_size - offset))
            async with self._global_semaphore:
                payload = await asyncio.to_thread(
                    self._read_part,
                    source,
                    offset,
                    expected,
                )
                if len(payload) != expected:
                    raise ValueError(
                        f"short local read for Telegram upload part {part_index}: "
                        f"{len(payload)}/{expected}"
                    )
                if is_big:
                    request = functions.upload.SaveBigFilePartRequest(
                        file_id=file_id,
                        file_part=part_index,
                        file_total_parts=part_count,
                        bytes=payload,
                    )
                else:
                    request = functions.upload.SaveFilePartRequest(
                        file_id=file_id,
                        file_part=part_index,
                        bytes=payload,
                    )
                result = await self._client(request)
                if not result:
                    raise RuntimeError(f"Telegram rejected upload part {part_index}")

        async def worker(worker_index: int) -> None:
            for part_index in range(worker_index, part_count, workers):
                await send_part(part_index)

        tasks = [
            asyncio.create_task(worker(worker_index), name=f"tgvio-upload-part-worker-{worker_index}")
            for worker_index in range(workers)
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        if is_big:
            return types.InputFileBig(file_id, part_count, file_name)
        return custom.InputSizedFile(
            file_id,
            part_count,
            file_name,
            md5=hash_md5,
            size=file_size,
        )

    @staticmethod
    def _read_part(source: Path, offset: int, size: int) -> bytes:
        with source.open("rb") as handle:
            handle.seek(offset)
            return handle.read(size)

    @staticmethod
    def _md5_file(source: Path):
        digest = hashlib.md5()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest
