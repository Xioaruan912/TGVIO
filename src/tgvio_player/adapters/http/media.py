from __future__ import annotations

import math
import time

from aiohttp import web

from tgvio_player.application.favorite_backup import FavoriteBackupError

from .client import _prefetch_requested
from .diagnostics import fingerprint, log_event


class PlayerMediaHttpMixin:
    """Routes that concern exactly one catalog media item."""

    async def _media(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        details = await self._media_details(request.match_info["media_id"])
        return web.json_response(
            await self._media_dto(details, digest, prefetch=_prefetch_requested(request))
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
        await self._authenticate(request)
        self._require_same_origin(request)
        media_id = request.match_info["media_id"]
        media_fingerprint = fingerprint(media_id)
        request_id = request.get("player_request_id")
        await self._media_details(media_id)
        locations = await self._repository.active_location_records(media_id)
        if not locations:
            raise web.HTTPNotFound(text="media not found")

        variants = await self._repository.active_variants(media_id)
        variant_locations = [
            (str(v["variant_media_id"]), location)
            for v in variants
            for location in await self._repository.active_location_records(str(v["variant_media_id"]))
        ]
        targets = variant_locations + [(media_id, location) for location in locations]
        log_event(
            "media_delete_started",
            request_id=request_id,
            media=media_fingerprint,
            copies=len(targets),
        )

        deleted = 0
        failed = 0
        assert self._deleter is not None
        for copy_index, (target_id, (package_id, package_path, remote_relpath)) in enumerate(targets, start=1):
            if target_id == media_id and failed and variant_locations:
                failed += 1
                continue
            failure_kind = "DeleteReturnedFalse"
            try:
                succeeded = await self._deleter.delete_location(
                    package_path, remote_relpath
                )
            except Exception as exc:
                failure_kind = type(exc).__name__
                succeeded = False
            if not succeeded:
                failed += 1
                log_event(
                    "media_delete_copy_failed",
                    request_id=request_id,
                    media=media_fingerprint,
                    copy=copy_index,
                    error=failure_kind,
                )
                continue
            await self._repository.record_deleted_location(
                target_id, package_id, remote_relpath
            )
            if target_id != media_id:
                await self._repository.finalize_media_deletion(target_id)
                for cache in (self._range_cache, self._faststart, self._startup_cache):
                    discard = getattr(cache, "discard", None)
                    if callable(discard):
                        await discard(target_id)
            deleted += 1

        removed = await self._repository.finalize_media_deletion(media_id)
        log_event(
            "media_delete_repository_finalized",
            request_id=request_id,
            media=media_fingerprint,
            deleted_copies=deleted,
            failed_copies=failed,
            removed=removed,
        )
        if removed:
            if self._range_cache is not None:
                discard = getattr(self._range_cache, "discard", None)
                if callable(discard):
                    stage_started = time.monotonic()
                    await discard(media_id)
                    log_event("media_delete_cache_cleared", request_id=request_id, media=media_fingerprint,
                              cache="range", duration_ms=round((time.monotonic() - stage_started) * 1000, 1))
            if self._faststart is not None:
                discard = getattr(self._faststart, "discard", None)
                if callable(discard):
                    stage_started = time.monotonic()
                    await discard(media_id)
                    log_event("media_delete_cache_cleared", request_id=request_id, media=media_fingerprint,
                              cache="faststart", duration_ms=round((time.monotonic() - stage_started) * 1000, 1))
            stage_started = time.monotonic()
            await self._startup_cache.discard(media_id)
            log_event("media_delete_cache_cleared", request_id=request_id, media=media_fingerprint,
                      cache="startup_range", duration_ms=round((time.monotonic() - stage_started) * 1000, 1))
        return web.json_response(
            {
                "id": media_id,
                "deleted_copies": deleted,
                "failed_copies": failed,
                "removed": removed,
            }
        )

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
