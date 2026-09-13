from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Callable
from zoneinfo import ZoneInfo

from tgvio.application.ports import CacheOperator, JobRepository, PublishedMessageRemover
from tgvio.application.runtime_flags import RuntimeFlags
from tgvio.observability import log_event


_BEIJING = ZoneInfo("Asia/Shanghai")
_ROTATED_LOG_GLOB = "tgvio.jsonl.*"


class DailyMaintenanceService:
    """Clear the visible task list and local cache once per day.

    Keeps SQLite statistics (`daily_stats`) and operational JSONL logs (trimmed
    to a bounded retention); clears terminal Job history, the bot's status
    messages, and the download cache so the next day starts at 任务 #1.
    """

    def __init__(
        self,
        repository: JobRepository,
        *,
        cache_operator: CacheOperator | None = None,
        status_cleaner: PublishedMessageRemover | None = None,
        log_dir: Path | None = None,
        log_retention_days: int = 3,
    ) -> None:
        self._repository = repository
        self._cache_operator = cache_operator
        self._status_cleaner = status_cleaner
        self._log_dir = log_dir
        self._log_retention_days = max(1, int(log_retention_days))
        self._log = logging.getLogger("tgvio.maintenance")

    async def run(self) -> dict[str, int]:
        status_deleted = await self._delete_status_messages()
        cache_result = await self._clear_cache()
        purge = await self._repository.purge_terminal_history(reset_numbering=True)
        logs_removed = self._prune_logs()
        result = {
            "status_messages_deleted": status_deleted,
            "cache_removed": int(getattr(cache_result, "removed_dirs", 0) or 0),
            "jobs_deleted": int(purge.get("jobs_deleted", 0)),
            "tokens_deleted": int(purge.get("tokens_deleted", 0)),
            "outbox_deleted": int(purge.get("outbox_deleted", 0)),
            "logs_removed": logs_removed,
        }
        log_event(
            self._log,
            logging.INFO,
            "maintenance.daily.completed",
            status_messages_deleted=result["status_messages_deleted"],
            cache_removed=result["cache_removed"],
            jobs_deleted=result["jobs_deleted"],
            tokens_deleted=result["tokens_deleted"],
            outbox_deleted=result["outbox_deleted"],
            logs_removed=result["logs_removed"],
        )
        return result

    async def _delete_status_messages(self) -> int:
        if self._status_cleaner is None:
            return 0
        try:
            messages = await self._repository.list_job_display_messages()
        except Exception:
            return 0
        deleted = 0
        for entry in messages:
            try:
                await self._status_cleaner.delete_message(
                    int(entry["chat_id"]),
                    int(entry["message_id"]),
                )
                deleted += 1
            except Exception:
                continue
        return deleted

    async def _clear_cache(self):
        if self._cache_operator is None:
            return None
        try:
            return await self._cache_operator.cleanup(force=True)
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "maintenance.cache.failed",
                exception_type=type(exc).__name__,
            )
            return None

    def _prune_logs(self) -> int:
        if self._log_dir is None or not self._log_dir.is_dir():
            return 0
        cutoff = time.time() - self._log_retention_days * 86400
        removed = 0
        for path in self._log_dir.glob(_ROTATED_LOG_GLOB):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


class DailyMaintenanceRuntime:
    """Run the daily maintenance at a fixed Asia/Shanghai wall-clock time."""

    def __init__(
        self,
        service: DailyMaintenanceService,
        flags: RuntimeFlags,
        *,
        default_hour: int = 6,
        default_minute: int = 0,
        poll_seconds: float = 60.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._service = service
        self._flags = flags
        self._default_hour = int(default_hour)
        self._default_minute = int(default_minute)
        self._poll_seconds = max(5.0, float(poll_seconds))
        self._now = now or time.time
        self._last_run_day: str | None = None
        self._task: asyncio.Task | None = None
        self._log = logging.getLogger("tgvio.maintenance.runtime")

    async def start(self) -> None:
        if self._task is not None:
            return
        await self._maybe_run()
        self._task = asyncio.create_task(self._run(), name="tgvio-daily-maintenance")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _maybe_run(self) -> None:
        if not self._flags.bool("daily_cleanup_enabled", True):
            return
        now = datetime.fromtimestamp(float(self._now()), tz=timezone.utc).astimezone(_BEIJING)
        hour, minute = self._scheduled_time()
        today_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        day_key = now.strftime("%Y-%m-%d")
        if now >= today_run and self._last_run_day != day_key:
            self._last_run_day = day_key
            try:
                await self._service.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "maintenance.daily.failed",
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )

    def _scheduled_time(self) -> tuple[int, int]:
        raw = self._flags.get("daily_cleanup_time", "06:00") or "06:00"
        try:
            hour_str, minute_str = str(raw).split(":", 1)
            hour = min(23, max(0, int(hour_str)))
            minute = min(59, max(0, int(minute_str)))
            return hour, minute
        except (ValueError, TypeError):
            return self._default_hour, self._default_minute

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                await self._maybe_run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "maintenance.runtime.failed",
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )
