from __future__ import annotations

from aiohttp import web

from tgvio_player.application.archive_read import ReadModeUnavailable


class PlayerReadModeHttpMixin:
    async def _read_mode_get(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        if self._read_mode is None:
            return web.json_response({"mode": "webdav", "direct_available": False})
        return web.json_response(self._read_mode.describe())

    async def _read_mode_put(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        payload = await self._json_object(request)
        mode = payload.get("mode")
        if mode not in {"webdav", "direct"}:
            raise web.HTTPBadRequest(text="invalid read mode")
        if self._read_mode is None:
            raise web.HTTPConflict(text="direct reads are not configured")
        try:
            await self._read_mode.choose(mode)
        except ReadModeUnavailable:
            raise web.HTTPConflict(text="direct reads are not configured") from None
        return web.json_response(self._read_mode.describe())
