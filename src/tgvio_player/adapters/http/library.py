"""The library's catalogue surface: dates, folders, one folder's videos, and collections.

Collections are user-owned rows from ``sqlite_collections``. Favourites are not
mirrored into that table: "favorites" is a builtin collection the API adds, read from
the same authority the favourites page uses, and it refuses every write.
"""
from __future__ import annotations

from aiohttp import web

from tgvio_player.domain.collection import SMART, Collection
from tgvio_player.domain.library_filters import LibraryFilters, parse_rules
from tgvio_player.infrastructure.video_query import listing_query_kwargs

BUILTIN_FAVORITES_ID = "favorites"
BUILTIN_FAVORITES_NAME = "收藏"

# A count is a badge, not a report: it must never walk the whole library. When a smart
# collection matches more than this, the client is told the number is a floor.
_MAX_COLLECTION_COUNT = 1000
# The same ceiling the wall uses for one browse session. It bounds a collection page
# the way ``MAX_ROWS`` bounds the frame wall, so neither can be asked to scan on.
_MAX_ITEMS_OFFSET = 1000
_MAX_ITEMS_PAGE = 60
_DEFAULT_ITEMS_PAGE = 20

_CREATE_FIELDS = frozenset({"name", "kind", "rules_json"})
_PATCH_FIELDS = frozenset({"name", "rules_json"})
_UNSET = object()


class PlayerLibraryHttpMixin:
    """Authenticated catalog metadata only; never prepares or schedules media."""

    def _register_library_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/library/dates", self._library_dates)
        app.router.add_get("/api/v1/library/folders", self._library_folders)
        app.router.add_get("/api/v1/library/videos", self._library_videos)
        app.router.add_get("/api/v1/collections", self._collections)
        app.router.add_post("/api/v1/collections", self._collection_create)
        app.router.add_patch("/api/v1/collections/{collection_id}", self._collection_update)
        app.router.add_delete("/api/v1/collections/{collection_id}", self._collection_delete)
        app.router.add_get("/api/v1/collections/{collection_id}/items", self._collection_items)
        app.router.add_put(
            "/api/v1/collections/{collection_id}/items/{media_id}", self._collection_item_add
        )
        app.router.add_delete(
            "/api/v1/collections/{collection_id}/items/{media_id}", self._collection_item_remove
        )

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

    # --- collections ---------------------------------------------------------

    @staticmethod
    def _collection_fields(collection: Collection) -> dict[str, object]:
        return {
            "collection_id": collection.collection_id,
            "name": collection.name,
            "kind": collection.kind,
            "rules_json": collection.rules_json,
        }

    @staticmethod
    def _collection_page_limit(request: web.Request) -> int:
        try:
            limit = int(request.query.get("limit", str(_DEFAULT_ITEMS_PAGE)))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid paging") from None
        if not 1 <= limit <= _MAX_ITEMS_PAGE:
            raise web.HTTPBadRequest(text="invalid paging")
        return limit

    @staticmethod
    def _collection_offset(raw: str | None) -> int:
        if raw is None or raw == "":
            return 0
        if not raw.isdigit() or int(raw) > _MAX_ITEMS_OFFSET:
            raise web.HTTPBadRequest(text="invalid cursor")
        return int(raw)

    @staticmethod
    def _collection_create_body(payload: dict[str, object]) -> tuple[str, str, str | None]:
        if any(key not in _CREATE_FIELDS for key in payload):
            raise web.HTTPBadRequest(text="unknown collection field")
        name, kind = payload.get("name"), payload.get("kind")
        rules = payload.get("rules_json")
        if not isinstance(name, str) or not isinstance(kind, str):
            raise web.HTTPBadRequest(text="invalid collection")
        if rules is not None and not isinstance(rules, str):
            raise web.HTTPBadRequest(text="invalid collection rules")
        return name, kind, rules

    @staticmethod
    def _collection_patch_body(payload: dict[str, object]) -> tuple[object, object]:
        if any(key not in _PATCH_FIELDS for key in payload):
            raise web.HTTPBadRequest(text="unknown collection field")
        name = payload.get("name", _UNSET)
        rules = payload.get("rules_json", _UNSET)
        if name is _UNSET and rules is _UNSET:
            raise web.HTTPBadRequest(text="nothing to update")
        if name is not _UNSET and not isinstance(name, str):
            raise web.HTTPBadRequest(text="invalid collection name")
        if rules is not _UNSET and rules is not None and not isinstance(rules, str):
            raise web.HTTPBadRequest(text="invalid collection rules")
        return name, rules

    async def _favorite_page(
        self, digest: str, *, limit: int, before: tuple[int, str] | None
    ) -> list[tuple[str, int]]:
        """The favourites page's own authority, so the builtin cannot disagree with it."""
        if self._favorite_backup is not None:
            return await self._repository.list_global_favorite_page(limit=limit, before=before)
        return await self._repository.list_favorite_page(digest, limit=limit, before=before)

    def _collection_query_kwargs(self, filters: LibraryFilters, digest: str) -> dict[str, object]:
        return listing_query_kwargs(
            filters,
            category="all",
            large_video_seconds=self._large_video_seconds,
            sort=filters.sort,
            favorite_scope="global" if self._favorite_backup is not None else "session",
            favorite_token_digest=digest,
        )

    async def _smart_member_ids(
        self, rules_json: str | None, digest: str, *, limit: int, offset: int
    ) -> list[str]:
        filters = parse_rules(rules_json)
        if filters == LibraryFilters.empty():
            # An unreadable or empty rule blob is an *empty collection*. Reading it
            # as "no conditions" would be the whole library instead, which is how a
            # single corrupt row becomes a privacy surprise.
            return []
        return await self._repository.list_video_ids(
            **self._collection_query_kwargs(filters, digest), limit=limit, offset=offset
        )

    async def _collection_size(
        self, collection: Collection, digest: str, counted: dict[str, int]
    ) -> tuple[int, bool]:
        if collection.kind == SMART:
            ids = await self._smart_member_ids(
                collection.rules_json, digest,
                limit=_MAX_COLLECTION_COUNT + 1, offset=0,
            )
            matches = len(ids)
        else:
            matches = int(counted.get(collection.collection_id, 0))
        return min(matches, _MAX_COLLECTION_COUNT), matches > _MAX_COLLECTION_COUNT

    async def _collection_dtos(self, media_ids: list[str], digest: str) -> list[dict[str, object]]:
        # A listing is metadata only: no prefetch, no media request from the browser.
        items = []
        for media_id in media_ids:
            details = await self._repository.active_media_details(media_id)
            if details is not None:
                items.append(await self._media_dto(details, digest, prefetch=False))
        return items

    async def _collections(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        self._library_query(request, set())
        favorites = await self._favorite_page(
            digest, limit=_MAX_COLLECTION_COUNT + 1, before=None
        )
        items: list[dict[str, object]] = [
            {
                "collection_id": BUILTIN_FAVORITES_ID,
                "name": BUILTIN_FAVORITES_NAME,
                "kind": "builtin",
                "rules_json": None,
                "count": min(len(favorites), _MAX_COLLECTION_COUNT),
                "count_capped": len(favorites) > _MAX_COLLECTION_COUNT,
            }
        ]
        counted = await self._repository.counts()
        for collection in await self._repository.list():
            count, capped = await self._collection_size(collection, digest, counted)
            items.append({**self._collection_fields(collection), "count": count, "count_capped": capped})
        return web.json_response({"items": items})

    async def _collection_create(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        name, kind, rules = self._collection_create_body(await self._json_object(request))
        try:
            collection = await self._repository.create(name, kind, rules)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        return web.json_response(self._collection_fields(collection), status=201)

    async def _collection_update(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        collection_id = request.match_info["collection_id"]
        if collection_id == BUILTIN_FAVORITES_ID:
            raise web.HTTPBadRequest(text="the builtin collection is read-only")
        name, rules = self._collection_patch_body(await self._json_object(request))
        if await self._repository.get(collection_id) is None:
            raise web.HTTPNotFound(text="collection not found")
        try:
            if name is not _UNSET:
                await self._repository.rename(collection_id, name)
            if rules is not _UNSET:
                await self._repository.set_rules(collection_id, rules)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        updated = await self._repository.get(collection_id)
        return web.json_response(self._collection_fields(updated))

    async def _collection_delete(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        collection_id = request.match_info["collection_id"]
        if collection_id == BUILTIN_FAVORITES_ID:
            raise web.HTTPBadRequest(text="the builtin collection is read-only")
        if not await self._repository.delete(collection_id):
            raise web.HTTPNotFound(text="collection not found")
        return web.Response(status=204)

    async def _builtin_item_page(
        self, request: web.Request, digest: str, limit: int
    ) -> tuple[list[str], str | None]:
        raw_cursor = request.query.get("cursor")
        before = self._decode_favorite_cursor(raw_cursor) if raw_cursor else None
        if raw_cursor and before is None:
            raise web.HTTPBadRequest(text="invalid cursor")
        rows = await self._favorite_page(digest, limit=limit + 1, before=before)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = self._encode_favorite_cursor(rows[-1]) if has_more and rows else None
        return [media_id for media_id, _created_at in rows], next_cursor

    async def _collection_items(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        self._library_query(request, {"limit", "cursor"})
        limit = self._collection_page_limit(request)
        collection_id = request.match_info["collection_id"]
        if collection_id == BUILTIN_FAVORITES_ID:
            media_ids, next_cursor = await self._builtin_item_page(request, digest, limit)
            return web.json_response({
                "items": await self._collection_dtos(media_ids, digest),
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            })
        collection = await self._repository.get(collection_id)
        if collection is None:
            raise web.HTTPNotFound(text="collection not found")
        offset = self._collection_offset(request.query.get("cursor"))
        if collection.kind == SMART:
            media_ids = await self._smart_member_ids(
                collection.rules_json, digest, limit=limit + 1, offset=offset
            )
        else:
            media_ids = list(await self._repository.items(collection_id, limit + 1, offset))
        has_more = len(media_ids) > limit
        media_ids = media_ids[:limit]
        return web.json_response({
            "items": await self._collection_dtos(media_ids, digest),
            "has_more": has_more,
            "next_cursor": str(offset + len(media_ids)) if has_more else None,
        })

    async def _editable_collection(self, request: web.Request) -> str:
        await self._authenticate(request)
        self._require_same_origin(request)
        collection_id = request.match_info["collection_id"]
        if collection_id == BUILTIN_FAVORITES_ID:
            raise web.HTTPBadRequest(text="the builtin collection is read-only")
        collection = await self._repository.get(collection_id)
        if collection is None:
            raise web.HTTPNotFound(text="collection not found")
        if collection.kind == SMART:
            raise web.HTTPBadRequest(text="a smart collection has no hand-picked members")
        return collection_id

    async def _collection_item_add(self, request: web.Request) -> web.Response:
        collection_id = await self._editable_collection(request)
        # 404 unless this is an active video: the member list must never show a tile
        # the wall could not show either.
        await self._media_details(request.match_info["media_id"])
        await self._repository.add_item(collection_id, request.match_info["media_id"])
        return web.Response(status=204)

    async def _collection_item_remove(self, request: web.Request) -> web.Response:
        collection_id = await self._editable_collection(request)
        await self._repository.remove_item(collection_id, request.match_info["media_id"])
        return web.Response(status=204)
