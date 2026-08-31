"""Local-only runtime heartbeat used by Docker liveness/readiness checks."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path


class RuntimeHeartbeat:
    def __init__(self, path: str | os.PathLike[str], *, interval: float = 10.0) -> None:
        self.path = Path(path)
        self.interval = max(1.0, float(interval))
        self._ready = False
        self._task: asyncio.Task | None = None

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "pid": os.getpid(),
            "heartbeat_at": time.time(),
            "ready": bool(self._ready),
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    async def _run(self) -> None:
        try:
            while True:
                self._write()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._write()
        self._task = asyncio.get_running_loop().create_task(self._run())

    def set_ready(self, ready: bool) -> None:
        self._ready = bool(ready)
        self._write()

    async def stop(self) -> None:
        self._ready = False
        self._write()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
