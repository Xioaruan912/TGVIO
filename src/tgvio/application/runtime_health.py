from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from tgvio.application.ports import JobRepository
from tgvio.observability import log_event


class RuntimeHealthHeartbeat:
    """Persist liveness/connectivity heartbeats for external health checks."""

    def __init__(
        self,
        repository: JobRepository,
        telegram_connected: Callable[[], bool],
        *,
        interval_seconds: float = 10.0,
    ) -> None:
        self._repository = repository
        self._telegram_connected = telegram_connected
        self._interval_seconds = max(1.0, float(interval_seconds))
        self._task: asyncio.Task | None = None
        self._log = logging.getLogger("tgvio.runtime.health")

    async def start(self) -> None:
        if self._task is not None:
            return
        await self._tick()
        self._task = asyncio.create_task(self._run(), name="tgvio-runtime-heartbeat")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "runtime.health.heartbeat_failed",
                    "Runtime heartbeat update failed",
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )

    async def _tick(self) -> None:
        connected = bool(self._telegram_connected())
        await self._repository.set_runtime_health("runtime", "alive")
        await self._repository.set_runtime_health(
            "telegram",
            "connected" if connected else "disconnected",
        )
