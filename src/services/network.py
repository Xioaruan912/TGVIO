"""Serialized network/proxy coordination for concurrent workers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence


@dataclass(frozen=True)
class NetworkSwitchResult:
    switched: bool
    generation: int
    index: int
    reevaluate: bool = False


class NetworkCoordinator:
    """Own proxy reconnect serialization and network generations.

    A worker records ``generation`` before starting transport.  If another
    worker switches the proxy while that transport is running, the stale
    worker must retry on the new generation instead of immediately switching
    again and causing reconnect thrash.
    """

    def __init__(
        self,
        *,
        proxies: Callable[[], Sequence[dict]],
        current: Callable[[], int],
        auto_enabled: Callable[[], bool],
        apply_proxy: Callable[[int], Awaitable[bool]],
    ) -> None:
        self._proxies = proxies
        self._current = current
        self._auto_enabled = auto_enabled
        self._apply_proxy = apply_proxy
        self._lock = asyncio.Lock()
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    async def apply(self, index: int) -> NetworkSwitchResult:
        async with self._lock:
            ok = await self._apply_proxy(int(index))
            if ok:
                self._generation += 1
            return NetworkSwitchResult(
                switched=ok,
                generation=self._generation,
                index=int(index) if ok else int(self._current()),
            )

    async def auto_switch(
        self,
        *,
        observed_generation: int,
    ) -> NetworkSwitchResult:
        async with self._lock:
            if int(observed_generation) != self._generation:
                return NetworkSwitchResult(
                    switched=False,
                    generation=self._generation,
                    index=int(self._current()),
                    reevaluate=True,
                )
            proxies = list(self._proxies())
            if not proxies or not self._auto_enabled():
                return NetworkSwitchResult(False, self._generation, int(self._current()))
            current = int(self._current())
            order = [idx for idx in range(len(proxies)) if idx != current]
            for idx in order:
                if await self._apply_proxy(idx):
                    self._generation += 1
                    return NetworkSwitchResult(True, self._generation, idx)
            # Re-establish direct mode once after all configured proxies fail.
            if await self._apply_proxy(-1):
                self._generation += 1
            return NetworkSwitchResult(False, self._generation, int(self._current()))
