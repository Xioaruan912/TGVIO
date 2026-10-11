from __future__ import annotations

import re

from aiohttp import web

from tgvio_player.domain.duplicates import duplicate_groups

_MEDIA_ID = re.compile(r"^[0-9a-f]{64}$")
_MAX_GROUPS = 50
_MAX_DISMISS = 20


class PlayerDuplicateReviewHttpMixin:
    def _register_duplicate_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/duplicates", self._duplicates)
        app.router.add_post("/api/v1/duplicates/dismiss", self._duplicates_dismiss)

    async def _duplicates(self, request: web.Request) -> web.Response:
        """Groups of videos with near-identical covers and lengths, largest first."""
        digest = await self._authenticate(request)
        groups = duplicate_groups(
            await self._repository.duplicate_candidates(),
            await self._repository.dismissed_duplicate_pairs(),
        )
        payload = []
        for group in groups:
            items = []
            for media_id in group:
                details = await self._repository.active_media_details(media_id)
                if details is not None:
                    items.append(await self._media_dto(details, digest, prefetch=False))
            if len(items) > 1:
                items.sort(key=lambda item: (-int(item.get("size_bytes") or 0), item["id"]))
                payload.append({"items": items})
        payload.sort(key=lambda group: -sum(int(i.get("size_bytes") or 0) for i in group["items"]))
        return web.json_response({"groups": payload[:_MAX_GROUPS], "total": len(payload)})

    async def _duplicates_dismiss(self, request: web.Request) -> web.Response:
        """The viewer says these are not the same video: never group them again."""
        await self._authenticate(request)
        self._require_same_origin(request)
        payload = await self._json_object(request)
        media_ids = payload.get("media_ids")
        if (not isinstance(media_ids, list) or not 2 <= len(media_ids) <= _MAX_DISMISS
                or not all(isinstance(m, str) and _MEDIA_ID.fullmatch(m) for m in media_ids)):
            raise web.HTTPBadRequest(text="invalid media_ids")
        return web.json_response({"dismissed": await self._repository.dismiss_duplicates(media_ids)})
