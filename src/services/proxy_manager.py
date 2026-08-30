"""HTTP proxy settings facade for Telegram handlers."""

from __future__ import annotations

import asyncio
from typing import Any


class ProxyManager:
    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline

    def proxies(self) -> list[dict]:
        return self._pipeline.proxy_cfg.get("proxies", [])

    def current(self) -> int:
        return int(self._pipeline.proxy_cfg.get("current", -1))

    def auto_enabled(self) -> bool:
        return bool(self._pipeline.proxy_cfg.get("auto", True))

    def label(self, index: int) -> str:
        return self._pipeline._proxy_label(index)

    def parse(self, url: str):
        return self._pipeline._parse_proxy_url(url)

    def contains(self, url: str) -> bool:
        return any(proxy.get("url") == url for proxy in self.proxies())

    def toggle_auto(self) -> bool:
        value = not self.auto_enabled()
        self._pipeline.proxy_cfg["auto"] = value
        self._pipeline._save_proxy_cfg()
        return value

    async def apply(self, index: int) -> bool:
        return await self._pipeline._apply_proxy(index)

    async def test(self, url: str) -> bool:
        try:
            return await asyncio.to_thread(self._pipeline._test_http_proxy, url)
        except Exception:
            return False

    def add(self, url: str) -> int:
        proxies = self.proxies()
        proxies.append({"url": url})
        self._pipeline._save_proxy_cfg()
        return len(proxies) - 1

    async def remove(self, index: int) -> dict | None:
        proxies = self.proxies()
        if index < 0 or index >= len(proxies):
            return None
        removed = proxies.pop(index)
        current = self.current()
        if current == index:
            self._pipeline.proxy_cfg["current"] = -1
            await self._pipeline._apply_proxy(-1)
        elif current > index:
            self._pipeline.proxy_cfg["current"] = current - 1
            self._pipeline._save_proxy_cfg()
        else:
            self._pipeline._save_proxy_cfg()
        return removed

