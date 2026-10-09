from __future__ import annotations

import math

from aiohttp import web

from tgvio_player.application.favorite_backup import FavoriteBackupError

from .client import _prefetch_requested

# A similarity request is a bounded read: a personal library can outgrow any one request's
# budget, and a partial answer that says so beats a silent one.
_MAX_SIMILAR_SCAN = 1000
_MAX_SIMILAR_LIMIT = 60
_SIMILAR_THRESHOLD_CEILING = 32
_SIMILAR_THRESHOLD = 16
_DUPLICATE_DISTANCE = 6


class PlayerMediaHttpMixin:
    """Routes that concern exactly one catalog media item."""

    async def _media(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        details = await self._media_details(request.match_info["media_id"])
        return web.json_response(
            await self._media_dto(details, digest, prefetch=_prefetch_requested(request))
        )

    async def _similar(self, request: web.Request) -> web.Response:
        """What looks like this cover. The fingerprint never leaves the session."""
        digest = await self._authenticate(request)
        details = await self._media_details(request.match_info["media_id"])
        media_id = str(details["media_id"])
        try:
            limit = int(request.query.get("limit", "24"))
            threshold = int(request.query.get("threshold", str(_SIMILAR_THRESHOLD)))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid similarity paging") from None
        if not 1 <= limit <= _MAX_SIMILAR_LIMIT:
            raise web.HTTPBadRequest(text="invalid similarity paging")
        if not 0 <= threshold <= _SIMILAR_THRESHOLD_CEILING:
            # A wider band stops meaning "similar" and starts meaning "everything".
            raise web.HTTPBadRequest(text="invalid similarity paging")
        cover = await self._repository.active_cover(media_id)
        phash = cover.get("phash") if cover is not None else None
        if not isinstance(phash, str) or not phash:
            # No similarity information is an answer, not a failure.
            return web.json_response({"items": [], "threshold": threshold, "truncated": False})
        pairs, truncated = await self._repository.similar_cover_ids(
            phash, media_id=media_id, threshold=threshold, limit=limit,
            scan_limit=_MAX_SIMILAR_SCAN
        )
        items = []
        for candidate, distance in pairs:
            candidate_details = await self._repository.active_media_details(candidate)
            if candidate_details is None:
                continue
            item = await self._media_dto(candidate_details, digest, prefetch=False)
            items.append({**item, "distance": distance,
                          "duplicate": distance <= _DUPLICATE_DISTANCE})
        return web.json_response(
            {"items": items, "threshold": threshold, "truncated": truncated}
        )

    async def _favorite(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        self._require_same_origin(request)
        media_id = request.match_info["media_id"]
        await self._media_details(media_id)
        if self._favorite_backup is None:
            await self._deck.favorite(digest, media_id)
            status = "synced"
        else:
            try:
                result = await self._favorite_backup.favorite(media_id)
            except FavoriteBackupError:
                raise web.HTTPBadRequest(text="favorite could not be saved") from None
            status = result.sync_status
        return web.json_response({"id": media_id, "favorite": True, "sync_status": status})

    async def _unfavorite(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        self._require_same_origin(request)
        media_id = request.match_info["media_id"]
        if self._favorite_backup is None:
            await self._deck.unfavorite(digest, media_id)
            status = "synced"
        else:
            try:
                result = await self._favorite_backup.unfavorite(media_id)
            except FavoriteBackupError:
                raise web.HTTPBadGateway(text="favorite removal is pending") from None
            status = result.sync_status
        return web.json_response({"id": media_id, "favorite": False, "sync_status": status})

    async def _long_video_progress(self, request: web.Request) -> web.Response:
        session_digest = await self._authenticate(request)
        items = await self._repository.list_long_video_progress()
        recent_items: list[dict[str, object]] = []
        for media_id, position in items[:5]:
            try:
                details = await self._media_details(media_id)
            except web.HTTPNotFound:
                continue
            duration = details.get("duration_seconds")
            if (
                not isinstance(duration, (int, float))
                or position <= 10
                or position >= float(duration) - 30
            ):
                continue
            media = await self._media_dto(details, session_digest)
            recent_items.append({**media, "position_seconds": position})
            if len(recent_items) == 5:
                break
        return web.json_response(
            {
                "items": [
                    {"id": media_id, "position_seconds": position}
                    for media_id, position in items
                ],
                "recent_items": recent_items,
            }
        )

    async def _save_long_video_progress(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        details = await self._media_details(request.match_info["media_id"])
        duration = details.get("duration_seconds")
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) <= self._large_video_seconds
        ):
            raise web.HTTPNotFound(text="long video not found")
        payload = await self._json_object(request)
        position = payload.get("position_seconds")
        if (
            isinstance(position, bool)
            or not isinstance(position, (int, float))
            or not math.isfinite(float(position))
            or float(position) < 0
        ):
            raise web.HTTPBadRequest(text="invalid playback position")
        position_seconds = min(float(duration), float(position))
        # A nearly completed video should reopen at the beginning, not at its
        # last few seconds. Keep no stale resume marker after completion.
        if position_seconds >= float(duration) - min(30.0, float(duration) * 0.05):
            await self._repository.delete_long_video_progress(str(details["media_id"]))
            return web.json_response({"id": details["media_id"], "completed": True})
        await self._repository.save_long_video_progress(
            str(details["media_id"]), position_seconds
        )
        return web.json_response(
            {"id": details["media_id"], "position_seconds": position_seconds}
        )

    async def _delete_long_video_progress(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        details = await self._media_details(request.match_info["media_id"])
        duration = details.get("duration_seconds")
        if (
            not isinstance(duration, (int, float))
            or float(duration) <= self._large_video_seconds
        ):
            raise web.HTTPNotFound(text="long video not found")
        await self._repository.delete_long_video_progress(str(details["media_id"]))
        return web.json_response({"id": details["media_id"], "deleted": True})

    async def _delete_media(self, request: web.Request) -> web.Response:
        """Accept a permanent delete at once; the archive files go in the background."""
        await self._authenticate(request)
        self._require_same_origin(request)
        media_id = request.match_info["media_id"]
        undo_seconds = await self._media_deletions.request(media_id)
        if undo_seconds is None:
            raise web.HTTPNotFound(text="media not found")
        return web.json_response(
            {"id": media_id, "queued": True, "undo_seconds": undo_seconds}, status=202
        )

    async def _cancel_media_deletion(self, request: web.Request) -> web.Response:
        """Undo, only while the undo window is open and no file was touched."""
        await self._authenticate(request)
        self._require_same_origin(request)
        media_id = request.match_info["media_id"]
        restored = await self._media_deletions.cancel(media_id)
        return web.json_response(
            {"id": media_id, "restored": restored}, status=200 if restored else 409
        )

    async def _media_deletions_status(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        return web.json_response(await self._media_deletions.status())

    async def _media_deletions_retry(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        retried = await self._media_deletions.retry_now()
        return web.json_response({"retried": retried, **await self._media_deletions.status()})

    async def _discard_media_caches(self, media_id: str) -> None:
        """A removed media must not keep serving from any cache (and ends its streams)."""
        for cache in (self._range_cache, self._faststart, self._startup_cache):
            discard = getattr(cache, "discard", None)
            if callable(discard):
                await discard(media_id)

    async def _media_details(self, media_id: str) -> dict[str, object]:
        if len(media_id) != 64 or any(char not in "0123456789abcdef" for char in media_id):
            raise web.HTTPNotFound(text="media not found")
        details = await self._repository.active_media_details(media_id)
        if details is None or details.get("kind") != "video":
            raise web.HTTPNotFound(text="media not found")
        return details

    async def _media_dto(
        self,
        details: dict[str, object],
        session_digest: str,
        *,
        prefetch: bool = False,
    ) -> dict[str, object]:
        media_id = str(details["media_id"])
        duration = details.get("duration_seconds")
        is_long = isinstance(duration, (int, float)) and float(duration) > self._large_video_seconds
        stream_url = f"/api/v1/media/{media_id}/stream"
        if prefetch:
            stream_url += "?cache=1"
        groups = [
            {"id": group_id, "label": label}
            for group_id, label in await self._repository.list_media_groups(media_id)
        ]
        variants = []
        for variant in await self._repository.active_variants(media_id):
            variant_id = str(variant["variant_media_id"])
            variants.append(
                {
                    "id": variant_id,
                    "height": variant.get("height"),
                    "width": variant.get("width"),
                    "bitrate_bps": variant.get("bitrate_bps"),
                    "label": variant.get("label"),
                    "size_bytes": variant.get("size_bytes"),
                    "stream_url": f"/api/v1/media/{variant_id}/stream",
                }
            )
        # Optional versioned archive cover: the browser only ever receives a
        # same-origin route, never a WebDAV path or credential.
        cover = await self._repository.active_cover(media_id)
        return {
            "id": media_id,
            "width": details["width"],
            "height": details["height"],
            "duration_seconds": duration,
            "size_bytes": details.get("size_bytes"),
            "stream_url": stream_url,
            "cover_url": None if cover is None else f"/api/v1/media/{media_id}/cover?v={cover['version']}",
            # Only when the cover carries one: a missing fingerprint is "no similarity
            # information", never a zero hash that a client could mistake for a match.
            **({"phash": cover["phash"]}
               if cover is not None and isinstance(cover.get("phash"), str) and cover["phash"]
               else {}),
            "favorite": (
                await self._repository.is_global_favorite(media_id)
                if self._favorite_backup is not None
                else await self._repository.is_favorite(session_digest, media_id)
            ),
            "deletable": self._deleter is not None,
            "mime_type": details.get("mime_type"),
            "codec": details.get("codec"),
            "category": "long" if is_long else "short",
            "groups": groups,
            "variants": variants,
        }
