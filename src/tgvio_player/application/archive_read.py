"""Choose how archive media is read, and never let the fast path break playback."""

from __future__ import annotations

import logging

from tgvio_player.domain.read_mode import DEFAULT_READ_MODE, DIRECT, WEBDAV, parse_read_mode

_LOG = logging.getLogger("tgvio_player.read_mode")


class ReadModeUnavailable(RuntimeError):
    pass


class ArchiveReadRouter:
    """Routes archive reads through WebDAV or direct links, per the chosen mode.

    WebDAV is the default and always available. Direct reads are optional: when
    the direct reader raises or answers with anything but a usable range, the same
    request is served over WebDAV, so switching modes can only change speed.
    """

    def __init__(self, webdav: object, direct: object | None, *, mode: str = DEFAULT_READ_MODE) -> None:
        self._webdav = webdav
        self._direct = direct
        self._mode = WEBDAV
        self.direct_reads = 0
        self.direct_fallbacks = 0
        self.set_mode(mode if direct is not None else WEBDAV)

    @property
    def direct_available(self) -> bool:
        return self._direct is not None

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        mode = parse_read_mode(mode)
        if mode == DIRECT and self._direct is None:
            raise ReadModeUnavailable("direct reads are not configured")
        self._mode = mode

    async def open_range(self, package_path: str, remote_relpath: str, byte_range):
        if self._mode == DIRECT and self._direct is not None:
            try:
                response = await self._direct.open_range(package_path, remote_relpath, byte_range)
                if response.status in {200, 206, 416}:
                    self.direct_reads += 1
                    return response
                await _close(response)
                reason = f"status {response.status}"
            except Exception as exc:  # any direct failure falls back, never surfaces
                reason = type(exc).__name__
            self.direct_fallbacks += 1
            _LOG.info("player.read_mode.fallback reason=%s", reason)
        return await self._webdav.open_range(package_path, remote_relpath, byte_range)

    def stats(self) -> dict[str, object]:
        data: dict[str, object] = {
            "mode": self._mode,
            "direct_available": self.direct_available,
            "direct_reads": self.direct_reads,
            "direct_fallbacks": self.direct_fallbacks,
        }
        stats = getattr(self._direct, "stats", None)
        if callable(stats):
            data.update(stats())
        return data


class ReadModeService:
    """Persists the chosen mode and applies it to the router."""

    def __init__(self, repository: object, router: ArchiveReadRouter) -> None:
        self._repository = repository
        self._router = router

    async def load(self) -> str:
        stored = await self._repository.get_read_mode()
        try:
            self._router.set_mode(stored)
        except ReadModeUnavailable:
            # Direct was chosen while it was configured; it no longer is.
            self._router.set_mode(WEBDAV)
        return self._router.mode

    async def choose(self, mode: str) -> str:
        self._router.set_mode(mode)
        await self._repository.set_read_mode(self._router.mode)
        return self._router.mode

    def describe(self) -> dict[str, object]:
        return {"mode": self._router.mode, "direct_available": self._router.direct_available}

    def router_stats(self) -> dict[str, object]:
        return self._router.stats()


async def _close(response: object) -> None:
    close = getattr(getattr(response, "body", None), "aclose", None)
    if close is not None:
        await close()
