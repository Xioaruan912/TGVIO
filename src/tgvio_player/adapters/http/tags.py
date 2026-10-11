from __future__ import annotations

from aiohttp import web

from tgvio_player.domain.tags import InvalidTag, Tag


def _tag_json(tag: Tag) -> dict[str, object]:
    return {"id": tag.tag_id, "name": tag.name, "count": tag.count}


def _tag_id(request: web.Request) -> int:
    try:
        tag_id = int(request.match_info["tag_id"])
    except ValueError:
        raise web.HTTPNotFound(text="tag not found") from None
    if tag_id <= 0:
        raise web.HTTPNotFound(text="tag not found")
    return tag_id


class PlayerTagHttpMixin:
    def _register_tag_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/tags", self._tags)
        app.router.add_post("/api/v1/tags", self._tag_create)
        app.router.add_patch("/api/v1/tags/{tag_id}", self._tag_rename)
        app.router.add_delete("/api/v1/tags/{tag_id}", self._tag_delete)
        app.router.add_get("/api/v1/media/{media_id}/tags", self._media_tags)
        app.router.add_put("/api/v1/media/{media_id}/tags/{tag_id}", self._media_tag_add)
        app.router.add_delete("/api/v1/media/{media_id}/tags/{tag_id}", self._media_tag_remove)

    async def _tags(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        return web.json_response({"items": [_tag_json(t) for t in await self._repository.list_tags()]})

    async def _tag_name(self, request: web.Request) -> object:
        payload = await self._json_object(request)
        return payload.get("name")

    async def _tag_create(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        try:
            tag = await self._repository.create_tag(await self._tag_name(request))
        except InvalidTag as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        return web.json_response(_tag_json(tag), status=201)

    async def _tag_rename(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        try:
            renamed = await self._repository.rename_tag(_tag_id(request), await self._tag_name(request))
        except InvalidTag as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        if not renamed:
            raise web.HTTPNotFound(text="tag not found")
        return web.json_response({"renamed": True})

    async def _tag_delete(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        if not await self._repository.delete_tag(_tag_id(request)):
            raise web.HTTPNotFound(text="tag not found")
        return web.json_response({"deleted": True})

    async def _media_tags(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        details = await self._media_details(request.match_info["media_id"])
        return web.json_response({"ids": await self._repository.media_tag_ids(str(details["media_id"]))})

    async def _media_tag_set(self, request: web.Request, enabled: bool) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        details = await self._media_details(request.match_info["media_id"])
        if not await self._repository.set_media_tag(str(details["media_id"]), _tag_id(request), enabled=enabled):
            raise web.HTTPNotFound(text="tag not found")
        return web.json_response({"ids": await self._repository.media_tag_ids(str(details["media_id"]))})

    async def _media_tag_add(self, request: web.Request) -> web.Response:
        return await self._media_tag_set(request, True)

    async def _media_tag_remove(self, request: web.Request) -> web.Response:
        return await self._media_tag_set(request, False)
