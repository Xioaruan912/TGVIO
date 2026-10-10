from __future__ import annotations

from aiohttp import web

from tgvio_player.domain.watched import FORGET_CHOICES, parse_forget_after_days


class PlayerWatchedHttpMixin:
    def _register_watched_routes(self, app: web.Application) -> None:
        app.router.add_put("/api/v1/media/{media_id}/watched", self._mark_watched)
        app.router.add_get("/api/v1/settings/watched", self._watch_settings_get)
        app.router.add_put("/api/v1/settings/watched", self._watch_settings_put)

    async def _mark_watched(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        media_id = request.match_info["media_id"]
        if not await self._repository.mark_watched(media_id):
            raise web.HTTPNotFound(text="media not found")
        return web.json_response({"id": media_id, "watched": True})

    async def _watch_settings(self) -> web.Response:
        return web.json_response({
            "forget_after_days": await self._repository.get_forget_after_days(),
            "choices": list(FORGET_CHOICES),
        })

    async def _watch_settings_get(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        return await self._watch_settings()

    async def _watch_settings_put(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        payload = await self._json_object(request)
        try:
            days = parse_forget_after_days(payload.get("forget_after_days"))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid forget_after_days") from None
        await self._repository.set_forget_after_days(days)
        return await self._watch_settings()
