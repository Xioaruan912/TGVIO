from __future__ import annotations

from aiohttp import web


class PlayerLibraryHttpMixin:
    """Authenticated catalog metadata only; never prepares or schedules media."""

    def _register_library_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/library/dates", self._library_dates)
        app.router.add_get("/api/v1/library/folders", self._library_folders)
        app.router.add_get("/api/v1/library/videos", self._library_videos)

    @staticmethod
    def _library_query(request, allowed):
        if any(key not in allowed or len(request.query.getall(key)) != 1 for key in request.query):
            raise web.HTTPBadRequest(text="invalid library filter")

    async def _library_dates(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._library_query(request, set())
        return web.json_response(await self._repository.library_dates())

    async def _library_folders(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._library_query(request, {"date", "media_id"})
        try:
            data = await self._repository.library_folders(
                date_filter=request.query.get("date"), media_id=request.query.get("media_id"))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid library filter") from None
        return web.json_response(data)

    async def _library_videos(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        self._library_query(request, {"folder_id", "category", "limit", "cursor"})
        try:
            page = await self._repository.library_video_page(
                request.query.get("folder_id", ""), category=request.query.get("category", "all"),
                limit=int(request.query.get("limit", "20")), cursor=request.query.get("cursor"),
                long_seconds=self._large_video_seconds)
        except ValueError:
            raise web.HTTPBadRequest(text="invalid library paging") from None
        if page is None:
            raise web.HTTPNotFound(text="folder not found")
        ids = page.pop("media_ids")
        items = []
        for media_id in ids:
            details = await self._repository.active_media_details(media_id)
            if details is not None:
                items.append(await self._media_dto(details, digest, prefetch=False))
        return web.json_response({"items": items, **page})
