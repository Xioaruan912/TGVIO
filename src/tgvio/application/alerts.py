from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from tgvio.application.ports import JobRepository
from tgvio.domain.notifications import NotificationEvent
from tgvio.observability import log_event


class AlertRuntime:
    """Durably enqueue runtime failure/anomaly alerts, with transition + cooldown dedupe.

    It only records redacted facts into the outbox; delivery (owner Telegram chat
    and/or webhook) is handled by the notification runtime.
    """

    def __init__(
        self,
        repository: JobRepository,
        *,
        telegram_connected: Callable[[], bool],
        disk_free_bytes: Callable[[], int | None],
        reserve_bytes: int,
        cooldown_seconds: int = 3600,
        poll_seconds: float = 60.0,
        max_attempts: int = 5,
        now: Callable[[], float] | None = None,
        enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._repository = repository
        self._telegram_connected = telegram_connected
        self._disk_free_bytes = disk_free_bytes
        self._reserve_bytes = max(0, int(reserve_bytes))
        self._cooldown = max(60, int(cooldown_seconds))
        self._poll_seconds = max(5.0, float(poll_seconds))
        self._max_attempts = max(1, int(max_attempts))
        self._now = now or time.time
        self._enabled = enabled
        self._last_healthy: dict[str, bool] = {}
        self._last_bucket: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._log = logging.getLogger("tgvio.alerts")

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.run_once()
        self._task = asyncio.create_task(self._run(), name="tgvio-alert-runtime")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def run_once(self) -> dict[str, int]:
        if self._enabled is not None and not self._enabled():
            return {"enqueued": 0}
        now = float(self._now())
        enqueued = 0
        enqueued += await self._condition(
            key="telegram",
            healthy=bool(self._telegram_connected()),
            bad_event="runtime.telegram_disconnected",
            good_event="runtime.telegram_recovered",
            now=now,
            payload={"component": "telegram"},
        )
        free = self._disk_free_bytes()
        if free is not None and self._reserve_bytes > 0:
            enqueued += await self._condition(
                key="disk",
                healthy=int(free) >= self._reserve_bytes,
                bad_event="runtime.disk_low",
                good_event="runtime.disk_recovered",
                now=now,
                payload={
                    "component": "disk",
                    "free_bytes": int(free),
                    "reserve_bytes": self._reserve_bytes,
                },
            )
        return {"enqueued": enqueued}

    async def _condition(
        self,
        *,
        key: str,
        healthy: bool,
        bad_event: str,
        good_event: str,
        now: float,
        payload: dict[str, object],
    ) -> int:
        previous = self._last_healthy.get(key)
        transition = previous is not healthy
        self._last_healthy[key] = healthy
        if healthy:
            if previous is False:
                return await self._enqueue(
                    good_event,
                    f"alert:{key}:good:{int(now)}",
                    {"condition": "recovered", "occurred_at": int(now), **payload},
                    now,
                )
            return 0
        bucket = int(now) // self._cooldown
        if transition or self._last_bucket.get(key) != bucket:
            self._last_bucket[key] = bucket
            return await self._enqueue(
                bad_event,
                f"alert:{key}:bad:{bucket}",
                {"condition": "unhealthy", "occurred_at": int(now), **payload},
                now,
            )
        return 0

    async def _enqueue(
        self,
        event_type: str,
        dedupe_key: str,
        payload: dict[str, object],
        now: float,
    ) -> int:
        event = NotificationEvent(
            event_type=event_type,
            dedupe_key=dedupe_key,
            payload=payload,
            max_attempts=self._max_attempts,
        )
        enqueued = await self._repository.enqueue_notification(event, now=now)
        if enqueued:
            log_event(
                self._log,
                logging.WARNING if "recovered" not in event_type else logging.INFO,
                "alert.enqueued",
                event_type=event_type,
            )
        return 1 if enqueued else 0

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "alert.runtime.failed",
                    "Alert runtime iteration failed",
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )
