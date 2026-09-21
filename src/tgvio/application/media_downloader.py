from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import shutil
from pathlib import Path
from typing import Any

from tgvio.application.job_control import JobCancelRequested, JobControlService, JobHoldRequested
from tgvio.application.ports import JobRepository, MediaDownloader
from tgvio.domain.job import (
    DOWNLOAD_SKIPPED_CODE_KEY,
    DOWNLOAD_SKIPPED_KEY,
    Job,
    JobState,
    MediaKind,
    item_download_skipped,
)
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event

DOWNLOAD_SKIPPED_POLICY_KEY = "download_skipped"

_TELEGRAM_TIMEOUT_TYPES = {"TimeoutError", "TimedoutError", "TimedOutError", "TimedOut"}
_TELEGRAM_TIMEOUT_MARKERS = (
    "timeout while fetching data",
    "request was unsuccessful",
    "timed out",
)
_MISSING_MEDIA_MARKERS = (
    "has no telegram source ids",
    "telegram source message missing",
    "preview source unavailable",
)


def classify_download_error(exc: BaseException) -> tuple[str, str]:
    """Map a download exception to a stable code plus a user-facing sentence.

    Telethon hides the real cause behind ``Request was unsuccessful N time(s)``
    when the last-call error is not raised, and Telegram answers ``Timeout while
    fetching data`` (GetFileRequest) for media its storage cannot serve. Both are
    reported as a retryable Telegram-side fetch failure so the failure center can
    say something actionable instead of the raw string.
    """

    text = f"{type(exc).__name__}: {exc}".casefold()
    if any(marker in text for marker in _MISSING_MEDIA_MARKERS):
        return (
            "source_missing",
            "Telegram 上找不到这条媒体（可能已被来源删除）",
        )
    if type(exc).__name__ in _TELEGRAM_TIMEOUT_TYPES or any(
        marker in text for marker in _TELEGRAM_TIMEOUT_MARKERS
    ):
        return (
            "telegram_file_timeout",
            "Telegram 反复取用该文件失败（存储侧超时）；稍后会自动重试，或跳过这一项",
        )
    return ("download_failed", "下载失败")


class DiskSpaceLowError(RuntimeError):
    pass


class DownloadItemsUnavailableError(RuntimeError):
    """Every media item of a Job was skipped, so the Job fails as a whole."""


class JobDownloader:
    def __init__(
        self,
        repository: JobRepository,
        downloader: MediaDownloader,
        download_root: Path,
        *,
        reserve_bytes: int,
        control: JobControlService | None = None,
        item_attempts: int = 2,
        item_tolerance: bool = True,
        item_retry_delay_seconds: float = 5.0,
    ) -> None:
        self._repository = repository
        self._downloader = downloader
        self._download_root = download_root
        self._reserve_bytes = max(0, reserve_bytes)
        self._control = control
        self._item_attempts = max(1, int(item_attempts))
        self._item_tolerance = bool(item_tolerance)
        self._item_retry_delay = max(0.0, float(item_retry_delay_seconds))
        self._log = logging.getLogger("tgvio.download")

    async def _download_item_with_retries(self, job: Job, item, target_dir: Path):
        """Fetch one item, retrying the item itself before giving up on it."""

        last_error: BaseException | None = None
        for attempt in range(1, self._item_attempts + 1):
            download_item = self._with_url_options(job, item)
            try:
                return await self._download_with_cancel(job, download_item, target_dir)
            except (JobCancelRequested, JobHoldRequested):
                raise
            except Exception as exc:  # noqa: BLE001 - retried or reported below
                last_error = exc
                if attempt >= self._item_attempts:
                    break
                log_event(
                    self._log,
                    logging.WARNING,
                    "download.item.retry",
                    "Retrying one media item before skipping it",
                    job_id=job.id,
                    item_index=item.index,
                    attempt=attempt,
                    exception_type=type(exc).__name__,
                )
                if self._item_retry_delay:
                    await asyncio.sleep(self._item_retry_delay)
        assert last_error is not None
        raise last_error

    async def download(self, job: Job) -> Job:
        await self._safe_checkpoint(job, "paused before download started")
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
            skipped: list[dict[str, Any]] = []
            for position, item in enumerate(job.items):
                await self._safe_checkpoint(
                    job,
                    f"paused before download item {item.index}",
                )
                if item.kind == MediaKind.TEXT:
                    continue
                self._ensure_disk_capacity(item.size_bytes)
                try:
                    downloaded[position] = await self._download_item_with_retries(
                        job,
                        item,
                        target_dir,
                    )
                except (JobCancelRequested, JobHoldRequested):
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad file must not sink the batch
                    if not self._item_tolerance:
                        raise
                    code, reason = classify_download_error(exc)
                    downloaded[position] = replace(
                        item,
                        metadata={
                            **item.metadata,
                            DOWNLOAD_SKIPPED_KEY: True,
                            DOWNLOAD_SKIPPED_CODE_KEY: code,
                        },
                    )
                    skipped.append(
                        {
                            "index": int(item.index),
                            "source_message_id": item.source_message_id,
                            "error_code": code,
                        }
                    )
                    job.items = list(downloaded)
                    job.policy[DOWNLOAD_SKIPPED_POLICY_KEY] = list(skipped)
                    await self._repository.save(job)
                    log_event(
                        self._log,
                        logging.WARNING,
                        "download.item.skipped",
                        "Media item could not be fetched and was skipped",
                        job_id=job.id,
                        item_index=item.index,
                        error_code=code,
                        exception_type=type(exc).__name__,
                    )
                    continue
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
                await self._safe_checkpoint(
                    job,
                    f"paused after download item {item.index}",
                )
            if skipped:
                await self._fail_when_nothing_downloaded(job, downloaded, skipped)
        except (JobCancelRequested, JobHoldRequested):
            raise
        except DownloadItemsUnavailableError:
            # The Job was already failed with a precise code; keep that record.
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

    async def _fail_when_nothing_downloaded(
        self,
        job: Job,
        downloaded: list,
        skipped: list[dict[str, Any]],
    ) -> None:
        """Fail loudly when every media item was skipped, instead of publishing nothing."""

        usable = [
            item
            for item in downloaded
            if item.kind == MediaKind.TEXT or not item_download_skipped(item)
        ]
        if any(item.kind != MediaKind.TEXT for item in usable):
            log_event(
                self._log,
                logging.WARNING,
                "download.job.partial",
                "Some media items were skipped; the rest will continue",
                job_id=job.id,
                skipped=len(skipped),
                remaining=len([item for item in usable if item.kind != MediaKind.TEXT]),
            )
            return
        codes = {str(entry.get("error_code") or "") for entry in skipped}
        error_code = (
            "telegram_file_timeout" if codes == {"telegram_file_timeout"} else "download_failed"
        )
        message = f"全部 {len(skipped)} 项都无法从 Telegram 取用"
        if error_code == "telegram_file_timeout":
            message += "（存储侧取用超时）"
        log_event(
            self._log,
            logging.ERROR,
            "download.job.failed",
            "Every media item was skipped",
            job_id=job.id,
            error_code=error_code,
            skipped=len(skipped),
        )
        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="failed",
                detail_code=error_code,
                item_total=len(job.items),
            )
        )
        await self._repository.transition(
            job.id,
            JobState.FAILED,
            event_type="download_failed",
            error_code=error_code,
            error_message=message,
        )
        raise DownloadItemsUnavailableError(message)

    async def _safe_checkpoint(self, job: Job, detail: str) -> None:
        if self._control is not None:
            await self._control.safe_checkpoint(job, detail=detail)

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

    @staticmethod
    def _with_url_options(job: Job, item: Any) -> Any:
        """Freeze the per-job yt-dlp snapshot onto the URL media item."""

        if str(item.metadata.get("source_type") or "").lower() != "url":
            return item
        if "ytdlp" in item.metadata:
            return item
        policy = job.policy.get("ytdlp")
        if not isinstance(policy, dict):
            return item
        metadata = dict(item.metadata)
        metadata["ytdlp"] = {
            "preset": policy.get("preset"),
            "audio_only": bool(policy.get("audio_only")),
        }
        return replace(item, metadata=metadata)

    def _ensure_disk_capacity(self, expected_bytes: int) -> None:
        self._download_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self._download_root).free
        required = self._reserve_bytes + max(0, expected_bytes)
        if free < required:
            raise DiskSpaceLowError(
                f"insufficient disk space: free={free} required={required}"
            )
