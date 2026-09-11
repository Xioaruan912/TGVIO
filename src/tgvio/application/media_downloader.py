from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from tgvio.application.job_control import JobCancelRequested, JobControlService
from tgvio.application.ports import JobRepository, MediaDownloader
from tgvio.domain.job import Job, JobState, MediaKind
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event


class DiskSpaceLowError(RuntimeError):
    pass


class JobDownloader:
    def __init__(
        self,
        repository: JobRepository,
        downloader: MediaDownloader,
        download_root: Path,
        *,
        reserve_bytes: int,
        control: JobControlService | None = None,
    ) -> None:
        self._repository = repository
        self._downloader = downloader
        self._download_root = download_root
        self._reserve_bytes = max(0, reserve_bytes)
        self._control = control
        self._log = logging.getLogger("tgvio.download")

    async def download(self, job: Job) -> Job:
        await self._cancel_checkpoint(job, "cancelled before download started")
        if job.state == JobState.RECEIVED:
            job = await self._repository.transition(
                job.id,
                JobState.DOWNLOADING,
                event_type="download_started",
            )
        elif job.state != JobState.DOWNLOADING:
            raise ValueError(f"job must be received/downloading before download: {job.state.value}")
        log_event(
            self._log,
            logging.INFO,
            "download.job.started",
            job_id=job.id,
            item_count=len(job.items),
            expected_bytes=sum(max(0, int(item.size_bytes or 0)) for item in job.items),
        )
        target_dir = self._download_root / f"job-{job.id}"
        target_dir.mkdir(parents=True, exist_ok=True)
        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="downloading",
                current=0,
                total=0,
                item_total=len(job.items),
            )
        )

        try:
            downloaded = list(job.items)
            for position, item in enumerate(job.items):
                await self._cancel_checkpoint(
                    job,
                    f"cancelled before download item {item.index}",
                )
                if item.kind == MediaKind.TEXT:
                    continue
                self._ensure_disk_capacity(item.size_bytes)
                downloaded[position] = await self._download_with_cancel(
                    job,
                    item,
                    target_dir,
                )
                job.items = list(downloaded)
                await self._repository.save(job)
                log_event(
                    self._log,
                    logging.INFO,
                    "download.item.completed",
                    job_id=job.id,
                    item_index=item.index,
                    item_kind=item.kind.value,
                    bytes_done=int(downloaded[position].size_bytes or 0),
                )
                await self._cancel_checkpoint(
                    job,
                    f"cancelled after download item {item.index}",
                )
        except JobCancelRequested:
            raise
        except DiskSpaceLowError as exc:
            log_event(
                self._log,
                logging.ERROR,
                "download.job.failed",
                "Disk reserve check failed",
                job_id=job.id,
                error_code="disk_low",
                exception_type=type(exc).__name__,
            )
            await self._repository.set_job_progress(
                JobProgress(
                    job_id=job.id,
                    phase="failed",
                    detail_code="disk_low",
                    item_total=len(job.items),
                )
            )
            await self._repository.transition(
                job.id,
                JobState.FAILED,
                event_type="download_failed",
                error_code="disk_low",
                error_message=str(exc),
            )
            raise
        except Exception as exc:
            log_event(
                self._log,
                logging.ERROR,
                "download.job.failed",
                "Download pipeline failed",
                job_id=job.id,
                error_code="download_failed",
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            await self._repository.set_job_progress(
                JobProgress(
                    job_id=job.id,
                    phase="failed",
                    detail_code="download_failed",
                    item_total=len(job.items),
                )
            )
            await self._repository.transition(
                job.id,
                JobState.FAILED,
                event_type="download_failed",
                error_code="download_failed",
                error_message=str(exc),
            )
            raise

        completed = await self._repository.transition(
            job.id,
            JobState.DOWNLOADED,
            event_type="download_completed",
        )
        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="downloaded",
                current=len(job.items),
                total=len(job.items),
                item_total=len(job.items),
            )
        )
        log_event(
            self._log,
            logging.INFO,
            "download.job.completed",
            job_id=job.id,
            item_count=len(job.items),
            bytes_done=sum(max(0, int(item.size_bytes or 0)) for item in job.items),
        )
        return completed

    async def _cancel_checkpoint(self, job: Job, detail: str) -> None:
        if self._control is not None:
            await self._control.checkpoint(job, detail=detail)

    async def _download_with_cancel(
        self,
        job: Job,
        item: Any,
        target_dir: Path,
    ):
        loop = asyncio.get_running_loop()
        pending_updates: set[asyncio.Task] = set()
        last = {"pct": -1, "time": 0.0, "current": -1}

        def progress_callback(current: int, total: int | None) -> None:
            current = max(0, int(current or 0))
            total_value = max(0, int(total or item.size_bytes or 0))
            now = loop.time()
            pct = int(current * 100 / total_value) if total_value > 0 else -1
            is_final = total_value > 0 and current >= total_value
            if not is_final:
                if pct >= 0 and last["pct"] >= 0 and pct < last["pct"] + 5 and now - last["time"] < 0.5:
                    return
                if pct < 0 and current == last["current"]:
                    return
            last.update({"pct": pct, "time": now, "current": current})
            update = asyncio.create_task(
                self._repository.set_job_progress(
                    JobProgress(
                        job_id=job.id,
                        phase="downloading",
                        current=current,
                        total=total_value,
                        item_index=item.index,
                        item_total=len(job.items),
                    )
                )
            )
            pending_updates.add(update)

        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="downloading",
                current=0,
                total=max(0, int(item.size_bytes or 0)),
                item_index=item.index,
                item_total=len(job.items),
            )
        )
        task = asyncio.create_task(
            self._downloader.download(item, target_dir, progress_callback),
            name=f"tgvio-download-{job.id}-{item.index}",
        )
        try:
            if self._control is None:
                result = await task
            else:
                while True:
                    done, _pending = await asyncio.wait({task}, timeout=0.5)
                    if task in done:
                        result = await task
                        break
                    try:
                        await self._control.checkpoint(
                            job,
                            detail=f"cancelled during download item {item.index}",
                        )
                    except JobCancelRequested:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise
            if pending_updates:
                await asyncio.gather(*tuple(pending_updates), return_exceptions=True)
            final_size = int(result.size_bytes or 0)
            await self._repository.set_job_progress(
                JobProgress(
                    job_id=job.id,
                    phase="downloading",
                    current=final_size,
                    total=final_size,
                    item_index=item.index,
                    item_total=len(job.items),
                )
            )
            return result
        except BaseException:
            if pending_updates:
                await asyncio.gather(*tuple(pending_updates), return_exceptions=True)
            raise

    def _ensure_disk_capacity(self, expected_bytes: int) -> None:
        self._download_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self._download_root).free
        required = self._reserve_bytes + max(0, expected_bytes)
        if free < required:
            raise DiskSpaceLowError(
                f"insufficient disk space: free={free} required={required}"
            )
