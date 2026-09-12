from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from pathlib import Path

from telethon import TelegramClient

from tgvio.application.ports import TransferProgressCallback
from tgvio.domain.job import MediaItem
from tgvio.observability import log_event


class TelethonMediaDownloader:
    def __init__(
        self,
        client: TelegramClient,
        *,
        download_workers: int = 8,
        part_size_kb: int = 512,
        shard_retries: int = 3,
    ) -> None:
        self._client = client
        self._download_workers = max(1, int(download_workers))
        self._request_size = max(64, int(part_size_kb)) * 1024
        self._shard_retries = max(0, int(shard_retries))
        self._log = logging.getLogger("tgvio.telegram.download")

    async def download(
        self,
        item: MediaItem,
        target_dir: Path,
        progress_callback: TransferProgressCallback | None = None,
    ) -> MediaItem:
        if item.source_chat_id is None or item.source_message_id is None:
            raise ValueError(f"media item {item.index} has no Telegram source ids")
        message = await self._client.get_messages(
            item.source_chat_id,
            ids=item.source_message_id,
        )
        if message is None or message.media is None:
            raise RuntimeError(f"Telegram source message missing for item {item.index}")

        file_info = getattr(message, "file", None)
        extension = str(getattr(file_info, "ext", "") or "")
        if extension and not extension.startswith("."):
            extension = f".{extension}"
        final_path = target_dir / f"{item.index:03d}-{item.source_message_id}{extension}"
        expected_size = int(item.size_bytes or getattr(file_info, "size", 0) or 0)

        if final_path.is_file():
            current_size = final_path.stat().st_size
            if current_size > 0 and (expected_size <= 0 or current_size == expected_size):
                return self._completed(item, final_path, current_size, reused=True)

        temp_path = target_dir / f".{final_path.name}.part"
        temp_path.unlink(missing_ok=True)
        try:
            if (
                expected_size > 0
                and self._download_workers > 1
                and callable(getattr(self._client, "iter_download", None))
            ):
                try:
                    await self._download_concurrent(
                        message.media,
                        temp_path,
                        expected_size,
                        progress_callback,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_event(
                        self._log,
                        logging.WARNING,
                        "telegram.download.concurrent_fallback",
                        "Concurrent Telegram download failed; falling back to one stream",
                        item_index=item.index,
                        exception_type=type(exc).__name__,
                    )
                    temp_path.unlink(missing_ok=True)
                    temp_path = await self._download_sequential(
                        message,
                        temp_path,
                        item.index,
                        progress_callback,
                    )
            else:
                temp_path = await self._download_sequential(
                    message,
                    temp_path,
                    item.index,
                    progress_callback,
                )
            if not temp_path.is_file() or temp_path.stat().st_size <= 0:
                raise RuntimeError(f"Telegram download produced an empty file for item {item.index}")
            if expected_size > 0 and temp_path.stat().st_size != expected_size:
                raise RuntimeError(
                    f"Telegram download size mismatch for item {item.index}: "
                    f"{temp_path.stat().st_size}/{expected_size}"
                )
            temp_path.replace(final_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return self._completed(item, final_path, final_path.stat().st_size, reused=False)

    async def _download_sequential(
        self,
        message,
        temp_path: Path,
        item_index: int,
        progress_callback: TransferProgressCallback | None,
    ) -> Path:
        downloaded = await self._client.download_media(
            message,
            file=str(temp_path),
            progress_callback=progress_callback,
        )
        if not downloaded:
            raise RuntimeError(
                f"Telegram download returned no file for item {item_index}"
            )
        return Path(downloaded)

    async def _download_concurrent(
        self,
        media,
        temp_path: Path,
        file_size: int,
        progress_callback: TransferProgressCallback | None,
    ) -> None:
        """Download one Telegram file with interleaved MTProto ranges.

        Telethon's normal ``download_media`` path is intentionally conservative
        and effectively single-stream. On high-latency VPS routes that leaves a
        large amount of bandwidth unused. Each worker below owns every Nth
        request-sized range, so workers can progress independently while the
        final file remains deterministic and atomic.

        A failed shard resumes from its first missing range rather than
        restarting the whole file or double-counting already persisted bytes.
        """

        request_size = self._request_size
        max_ranges = max(1, (file_size + request_size - 1) // request_size)
        workers = min(self._download_workers, max_ranges)
        stride = workers * request_size
        write_lock = asyncio.Lock()
        received = 0

        temp_path.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("w+b") as handle:
            handle.truncate(file_size)

            async def consume(worker: int) -> None:
                nonlocal received
                next_offset = worker * request_size
                if next_offset >= file_size:
                    return
                failures = 0
                while next_offset < file_size:
                    try:
                        iterator = self._client.iter_download(
                            media,
                            offset=next_offset,
                            stride=stride,
                            request_size=request_size,
                            file_size=file_size,
                        )
                        async for chunk in iterator:
                            payload = bytes(chunk)
                            if not payload:
                                continue
                            async with write_lock:
                                handle.seek(next_offset)
                                handle.write(payload)
                                received += len(payload)
                                if progress_callback is not None:
                                    progress_callback(min(received, file_size), file_size)
                            next_offset += stride
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if failures >= self._shard_retries:
                            raise
                        failures += 1
                        await asyncio.sleep(min(2.0, 0.5 * failures))

            results = await asyncio.gather(
                *(consume(worker) for worker in range(workers)),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise errors[0]
            handle.flush()

        if progress_callback is not None:
            progress_callback(file_size, file_size)

    @staticmethod
    def _completed(item: MediaItem, path: Path, size: int, *, reused: bool) -> MediaItem:
        metadata = dict(item.metadata)
        metadata.update(
            {
                "download_complete": True,
                "download_reused_local": reused,
            }
        )
        return replace(
            item,
            local_path=str(path),
            size_bytes=size,
            metadata=metadata,
        )
