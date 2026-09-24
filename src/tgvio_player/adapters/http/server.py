from __future__ import annotations

import asyncio
import base64
from collections import Counter
import json
import math
from pathlib import Path
import re
import time
from typing import Any
import uuid
from urllib.parse import urlsplit

from aiohttp import web

from .client import _prefetch_requested, resolve_client
from .diagnostics import client_fingerprint, fingerprint, log_event
from .streaming import PlayerHttpStreamingMixin

from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.application.playback import StartupRangeCache
from tgvio_player.domain.auth import token_digest
from tgvio_player.infrastructure.webdav_read import ReadOnlyWebDavAdapter


_MAX_JSON_BYTES = 4096
_MAX_FEED_LIMIT = 20
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_FAVORITE_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_MAX_RANDOM_CANDIDATES = 5
_MAX_HEAD_PREFETCH_ITEMS = 5
_DEFAULT_STARTUP_CACHE_ENTRIES = 32
_DEFAULT_STARTUP_CACHE_BYTES = 64 * 1024 * 1024
_DEFAULT_STARTUP_RANGE_BYTES = 2 * 1024 * 1024
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_FAILURE_WINDOW_SECONDS = 10 * 60
_LOGIN_LOCKOUT_SECONDS = 15 * 60
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; media-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
class PlayerHttpServer(PlayerHttpStreamingMixin):
    """Small authenticated HTTP boundary around Player-only services.

    This adapter intentionally accepts only Player repository IDs. It has no
    knowledge of Bot configuration, sessions, databases, or runtime paths.
    """

    def __init__(
        self,
        repository: object,
        sessions: SessionService,
        deck: ShuffleDeckService,
        reader: ReadOnlyWebDavAdapter,
        *,
        deleter: object | None = None,
        max_streams: int = 8,
        max_streams_per_client: int = 2,
        max_header_size: int = 8192,
        stream_chunk_size: int = 64 * 1024,
        startup_cache: StartupRangeCache | None = None,
        startup_range_bytes: int = _DEFAULT_STARTUP_RANGE_BYTES,
        static_dir: Path | None = None,
        faststart: object | None = None,
        range_cache: object | None = None,
        large_video_seconds: float = 300.0,
        warm_head_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if min(
            max_streams, max_streams_per_client, max_header_size, stream_chunk_size, startup_range_bytes
        ) < 1:
            raise ValueError("HTTP limits must be positive")
        self._repository = repository
        self._sessions = sessions
        self._deck = deck
        self._reader = reader
        self._deleter = deleter
        self._max_streams = max_streams
        self._stream_slots = asyncio.BoundedSemaphore(max_streams)
        self._max_streams_per_client = max_streams_per_client
        self._stream_clients: Counter[str] = Counter()
        self._stream_lock = asyncio.Lock()
        # Speculative preloads must never consume the playback budget. They use
        # a small bounded share of the global slots and are dropped first.
        self._max_preload = max(1, max_streams // 4)
        self._preload_active = 0
        self._foreground_waiters = 0
        self._max_header_size = max_header_size
        self._stream_chunk_size = stream_chunk_size
        self._startup_cache = startup_cache or StartupRangeCache(
            max_entries=_DEFAULT_STARTUP_CACHE_ENTRIES,
            max_bytes=_DEFAULT_STARTUP_CACHE_BYTES,
        )
        self._startup_range_bytes = startup_range_bytes
        self._static_dir = static_dir.resolve() if static_dir is not None and static_dir.is_dir() else None
        self._faststart = faststart
        self._range_cache = range_cache
        self._large_video_seconds = max(1.0, float(large_video_seconds))
        self._warm_head_bytes = max(1, int(warm_head_bytes))
        self._login_failures: dict[str, tuple[int, float, float]] = {}
        self._login_lock = asyncio.Lock()

    @property
    def active_playback_streams(self) -> int:
        """Global playback stream count, used to pause low-priority work."""
        return sum(self._stream_clients.values())

    @property
    def playback_saturated(self) -> bool:
        """True when no stream slot is free, so speculative work should pause."""
        return self._stream_slots.locked()

    async def _schedule_head_prefetch(self, media_id: str, details: dict[str, object]) -> None:
        if self._range_cache is None:
            return
        location = await self._repository.active_media_location(media_id)
        if location is None:
            return
        size = int(details.get("size_bytes") or 0)
        if size <= 0:
            return
        chunk_bytes = int(getattr(self._range_cache, "chunk_bytes", self._warm_head_bytes))
        head_bytes = min(self._warm_head_bytes, chunk_bytes)
        self._range_cache.prefetch_head(
            media_id,
            location[0],
            location[1],
            size,
            head_bytes,
            whole_below=head_bytes,
        )

    def application(self) -> web.Application:
        app = web.Application(client_max_size=_MAX_JSON_BYTES)
        app.on_response_prepare.append(self._security_headers)
        app.middlewares.append(self._request_limits)
        app.router.add_get("/healthz", self._healthz)
        app.router.add_post("/api/v1/auth/login", self._login)
        app.router.add_post("/api/v1/auth/logout", self._logout)
        app.router.add_get("/api/v1/feed", self._feed)
        app.router.add_get("/api/v1/random", self._random)
        app.router.add_get("/api/v1/videos", self._videos)
        app.router.add_get("/api/v1/groups/{group_id}/videos", self._group_videos)
        app.router.add_get("/api/v1/favorites", self._favorites)
        app.router.add_get("/api/v1/long-progress", self._long_video_progress)
        app.router.add_get("/api/v1/cache-stats", self._cache_stats)
        app.router.add_post("/api/v1/diagnostics/playback-event", self._playback_diagnostic)
        app.router.add_get("/api/v1/media/{media_id}", self._media)
        if self._deleter is not None:
            app.router.add_delete("/api/v1/media/{media_id}", self._delete_media)
        app.router.add_get("/api/v1/media/{media_id}/stream", self._stream)
        app.router.add_post("/api/v1/media/{media_id}/prepare", self._prepare)
        app.router.add_put("/api/v1/media/{media_id}/favorite", self._favorite)
        app.router.add_delete("/api/v1/media/{media_id}/favorite", self._unfavorite)
        app.router.add_put("/api/v1/media/{media_id}/progress", self._save_long_video_progress)
        app.router.add_delete("/api/v1/media/{media_id}/progress", self._delete_long_video_progress)
        if self._static_dir is not None:
            app.router.add_get("/", self._frontend_index)
            assets = self._static_dir / "assets"
            if assets.is_dir():
                app.router.add_static("/assets", assets, show_index=False, follow_symlinks=False)
            for public_asset in (
                "site.webmanifest",
                "apple-touch-icon.png",
                "player-icon-192.png",
                "player-icon-512.png",
                "player-icon.svg",
            ):
                async def serve_public_asset(
                    request: web.Request, name: str = public_asset
                ) -> web.FileResponse:
                    return await self._frontend_public_asset(request, name)

                app.router.add_get(f"/{public_asset}", serve_public_asset)
        return app

    def runner(self) -> web.AppRunner:
        """Create a runner with parser-level request-line and header bounds."""
        return web.AppRunner(
            self.application(),
            access_log=None,
            max_field_size=self._max_header_size,
            max_line_size=self._max_header_size,
        )

    @web.middleware
    async def _request_limits(self, request: web.Request, handler: Any) -> web.StreamResponse:
        request_id = uuid.uuid4().hex
        request["player_request_id"] = request_id
        started = time.monotonic()
        status = 500
        error_kind: str | None = None
        try:
            if sum(len(name) + len(value) for name, value in request.headers.items()) > self._max_header_size:
                raise web.HTTPRequestHeaderFieldsTooLarge()
            if request.content_length is not None and request.content_length > _MAX_JSON_BYTES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=_MAX_JSON_BYTES, actual_size=request.content_length
                )
            if request.method not in {"GET", "HEAD", "POST", "PUT", "DELETE"}:
                raise web.HTTPMethodNotAllowed(request.method, {"GET", "HEAD", "POST", "PUT", "DELETE"})
            if "token" in request.query or "access_token" in request.query:
                raise web.HTTPBadRequest(text="query tokens are not accepted")
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as exc:
            status = request.get("player_response_status", exc.status)
            error_kind = type(exc).__name__
            raise
        except asyncio.CancelledError:
            status = request.get("player_response_status", 499)
            error_kind = "RequestCancelled"
            raise
        except Exception as exc:
            status = request.get("player_response_status", status)
            error_kind = type(exc).__name__
            raise
        finally:
            route = request.match_info.route.resource
            route_name = route.canonical if route is not None else request.path
            client_id = client_fingerprint(resolve_client(request))
            media_id = request.match_info.get("media_id", "")
            log_event(
                "http_request",
                request_id=request_id,
                method=request.method,
                route=route_name,
                status=status,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                response_bytes=(
                    int(response.headers.get("Content-Length", "0"))
                    if "response" in locals() and response.headers.get("Content-Length", "0").isdigit()
                    else 0
                ),
                client=client_id,
                media=fingerprint(media_id) if _MEDIA_ID_RE.fullmatch(media_id) else None,
                range=(request.headers.get("Range", "")[:48]
                       if re.fullmatch(r"bytes=\d*-\d*", request.headers.get("Range", ""))
                       else None),
                preload=request.headers.get("X-TGVIO-Preload") == "1",
                error=error_kind,
            )

    async def _security_headers(self, request: web.Request, response: web.StreamResponse) -> None:
        response.headers.update(_SECURITY_HEADERS)
        request["player_response_status"] = response.status
        request_id = request.get("player_request_id")
        if request_id:
            response.headers["X-Request-ID"] = request_id
        path = request.path
        if path.startswith("/assets/"):
            # Vite emits content-hashed filenames, so these are immutable.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path == "/healthz" or path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Cache-Control"] = "no-cache"

    async def _healthz(self, request: web.Request) -> web.Response:
        del request
        try:
            active = await self._repository.count_active_videos()
        except Exception:
            raise web.HTTPServiceUnavailable(text="unavailable") from None
        return web.json_response({"status": "ok", "active_videos": active})

    async def _cache_stats(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        stats = self._range_cache.stats() if self._range_cache is not None else {}
        return web.json_response({"available": self._range_cache is not None, **stats})

    async def _playback_diagnostic(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        payload = await self._json_object(request)
        event = payload.get("event")
        media_id = payload.get("media_id")
        category = payload.get("category")
        if not isinstance(event, str) or event not in {
            "media_error", "media_probe", "media_retry", "media_skip", "media_unplayable_streak",
            "media_stall_warning", "media_stall_skip",
        }:
            raise web.HTTPBadRequest(text="invalid diagnostic event")
        if not isinstance(media_id, str) or not _MEDIA_ID_RE.fullmatch(media_id):
            raise web.HTTPBadRequest(text="invalid media id")
        if not isinstance(category, str) or category not in {"short", "long"}:
            raise web.HTTPBadRequest(text="invalid media category")
        numeric_fields: dict[str, int] = {}
        for name, minimum, maximum in (
            ("media_error_code", 0, 5),
            ("network_state", 0, 3),
            ("ready_state", 0, 4),
            ("retry", 0, 10),
            ("probe_status", 0, 599),
            ("failure_streak", 0, 1000),
        ):
            value = payload.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum:
                numeric_fields[name] = value
        log_event(
            "frontend_playback",
            request_id=request.get("player_request_id"),
            client=client_fingerprint(resolve_client(request)),
            media=fingerprint(media_id),
            category=category,
            action=event,
            **numeric_fields,
        )
        return web.Response(status=204)

    async def _frontend_index(self, request: web.Request) -> web.FileResponse:
        del request
        assert self._static_dir is not None
        index = self._static_dir / "index.html"
        if not index.is_file():
            raise web.HTTPNotFound(text="frontend unavailable")
        return web.FileResponse(index)

    async def _frontend_public_asset(self, request: web.Request, name: str) -> web.FileResponse:
        del request
        assert self._static_dir is not None
        asset = self._static_dir / name
        if not asset.is_file():
            raise web.HTTPNotFound(text="frontend asset unavailable")
        return web.FileResponse(asset)

    async def _login(self, request: web.Request) -> web.Response:
        client = resolve_client(request)
        if not await self._login_allowed(client):
            raise web.HTTPTooManyRequests(
                text="too many failed login attempts",
                headers={"Retry-After": str(_LOGIN_LOCKOUT_SECONDS)},
            )
        payload = await self._json_object(request)
        secret = payload.get("secret")
        cookie = await self._sessions.login(secret if isinstance(secret, str) else None)
        if cookie is None:
            await self._record_login_failure(client)
            raise web.HTTPUnauthorized(text="invalid credentials")
        await self._clear_login_failures(client)
        response = web.json_response({"authenticated": True})
        response.headers.add("Set-Cookie", cookie.set_cookie_value())
        return response

    async def _logout(self, request: web.Request) -> web.Response:
        self._require_same_origin(request)
        await self._sessions.logout(request.cookies.get("tgvio_player_session"))
        response = web.json_response({"authenticated": False})
        response.headers.add(
            "Set-Cookie",
            "tgvio_player_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict",
        )
        return response

    async def _feed(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        cursor = request.query.get("cursor")
        if cursor not in {None, ""}:
            raise web.HTTPBadRequest(text="cursor is not supported by this feed version")
        try:
            limit = int(request.query.get("limit", "5"))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid limit") from None
        if not 1 <= limit <= _MAX_FEED_LIMIT:
            raise web.HTTPBadRequest(text="invalid limit")
        prefetch = _prefetch_requested(request)
        media_ids = await self._deck.next_items(digest, limit=limit)
        items = []
        for index, media_id in enumerate(media_ids):
            details = await self._repository.active_media_details(media_id)
            if details is not None:
                if self._faststart is not None and index < 3:
                    self._faststart.schedule(media_id, details)
                if index < _MAX_HEAD_PREFETCH_ITEMS:
                    await self._schedule_head_prefetch(media_id, details)
                items.append(await self._media_dto(details, digest, prefetch=prefetch))
        return web.json_response({"items": items, "next_cursor": None})

    async def _random(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        try:
            limit = int(request.query.get("limit", str(_MAX_RANDOM_CANDIDATES)))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid random limit") from None
        if not 1 <= limit <= _MAX_RANDOM_CANDIDATES:
            raise web.HTTPBadRequest(text="invalid random limit")
        raw_excludes = request.query.getall("exclude", [])
        if len(raw_excludes) > 12 or any(
            len(media_id) != 64
            or any(char not in "0123456789abcdef" for char in media_id)
            for media_id in raw_excludes
        ):
            raise web.HTTPBadRequest(text="invalid random exclusions")
        media_ids = await self._deck.random_short_ids(limit=limit, exclude=set(raw_excludes))
        items = []
        for media_id in media_ids:
            details = await self._repository.active_media_details(media_id)
            if details is None:
                continue
            if self._faststart is not None:
                self._faststart.schedule(media_id, details)
            items.append(await self._media_dto(details, digest, prefetch=True))
        return web.json_response({"items": items, "category": "short"})

    async def _videos(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        category = request.query.get("category", "short")
        if category not in {"short", "long", "all"}:
            raise web.HTTPBadRequest(text="invalid category")
        search = request.query.get("search", "").strip().lower()
        if len(search) > 64 or any(char not in "0123456789abcdef" for char in search):
            raise web.HTTPBadRequest(text="invalid search")
        try:
            limit = int(request.query.get("limit", "20"))
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid paging") from None
        if not 1 <= limit <= _MAX_FEED_LIMIT or offset < 0:
            raise web.HTTPBadRequest(text="invalid paging")
        if category == "all":
            media_ids = await self._repository.list_video_ids(
                order="media_id",
                limit=limit + 1,
                offset=offset,
                **({"media_id_prefix": search} if search else {}),
            )
        elif category == "long":
            media_ids = await self._repository.list_video_ids(
                min_seconds=self._large_video_seconds,
                order="duration_desc",
                limit=limit + 1,
                offset=offset,
                **({"media_id_prefix": search} if search else {}),
            )
        else:
            media_ids = await self._repository.list_video_ids(
                max_seconds=self._large_video_seconds,
                order="media_id",
                limit=limit + 1,
                offset=offset,
                **({"media_id_prefix": search} if search else {}),
            )
        has_more = len(media_ids) > limit
        media_ids = media_ids[:limit]
        prefetch = _prefetch_requested(request)
        items = []
        for index, media_id in enumerate(media_ids):
            details = await self._repository.active_media_details(media_id)
            if details is not None:
                if category != "all":
                    if self._faststart is not None and index < 3:
                        self._faststart.schedule(media_id, details)
                    if index < _MAX_HEAD_PREFETCH_ITEMS:
                        await self._schedule_head_prefetch(media_id, details)
                items.append(await self._media_dto(details, digest, prefetch=prefetch))
        count_method = getattr(self._repository, "count_video_ids", None)
        if callable(count_method):
            count_options: dict[str, object] = {"media_id_prefix": search} if search else {}
            if category == "long":
                count_options["min_seconds"] = self._large_video_seconds
            elif category == "short":
                count_options["max_seconds"] = self._large_video_seconds
            total = await count_method(**count_options)
        elif category == "all" and not search:
            total = await self._repository.count_active_videos()
        else:
            total = None
        return web.json_response(
            {"items": items, "has_more": has_more, "category": category, "total": total}
        )

    async def _group_videos(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        group_id = request.match_info["group_id"]
        try:
            label = await self._repository.resolve_archive_group(group_id)
        except ValueError:
            raise web.HTTPBadRequest(text="invalid group") from None
        if label is None:
            raise web.HTTPNotFound(text="group not found")
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid paging") from None
        if not 1 <= limit <= _MAX_FEED_LIMIT:
            raise web.HTTPBadRequest(text="invalid paging")
        cursor = request.query.get("cursor") or None
        if cursor is not None and not _MEDIA_ID_RE.fullmatch(cursor):
            raise web.HTTPBadRequest(text="invalid paging")
        media_ids = await self._repository.list_group_video_ids(
            group_id, after_id=cursor, limit=limit + 1
        )
        has_more = len(media_ids) > limit
        media_ids = media_ids[:limit]
        prefetch = _prefetch_requested(request)
        items = []
        for index, media_id in enumerate(media_ids):
            details = await self._repository.active_media_details(media_id)
            if details is not None:
                if self._faststart is not None and index < 3:
                    self._faststart.schedule(media_id, details)
                if index < _MAX_HEAD_PREFETCH_ITEMS:
                    await self._schedule_head_prefetch(media_id, details)
                items.append(await self._media_dto(details, digest, prefetch=prefetch))
        next_cursor = media_ids[-1] if has_more and media_ids else None
        return web.json_response(
            {
                "items": items,
                "has_more": has_more,
                "next_cursor": next_cursor,
                "group": {"id": group_id, "label": label},
            }
        )

    async def _media(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        details = await self._media_details(request.match_info["media_id"])
        return web.json_response(
            await self._media_dto(details, digest, prefetch=_prefetch_requested(request))
        )

    async def _favorites(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        try:
            limit = int(request.query.get("limit", str(_MAX_FEED_LIMIT)))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid paging") from None
        if not 1 <= limit <= _MAX_FEED_LIMIT:
            raise web.HTTPBadRequest(text="invalid paging")
        raw_cursor = request.query.get("cursor")
        cursor = self._decode_favorite_cursor(raw_cursor) if raw_cursor is not None else None
        if raw_cursor is not None and cursor is None:
            raise web.HTTPBadRequest(text="invalid paging")
        prefetch = _prefetch_requested(request)
        rows = await self._deck.favorite_page(digest, limit=limit + 1, cursor=cursor)
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for media_id, _created_at in rows:
            details = await self._repository.active_media_details(media_id)
            if details is not None:
                items.append(await self._media_dto(details, digest, prefetch=prefetch))
        next_cursor = self._encode_favorite_cursor(rows[-1]) if has_more and rows else None
        return web.json_response(
            {"items": items, "has_more": has_more, "next_cursor": next_cursor}
        )

    @staticmethod
    def _encode_favorite_cursor(row: tuple[str, int]) -> str:
        media_id, created_at = row
        payload = json.dumps([created_at, media_id], separators=(",", ":")).encode("ascii")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_favorite_cursor(value: str) -> tuple[int, str] | None:
        if not _FAVORITE_CURSOR_RE.fullmatch(value):
            return None
        try:
            raw = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
            parsed = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(parsed, list)
            or len(parsed) != 2
            or isinstance(parsed[0], bool)
            or not isinstance(parsed[0], int)
            or parsed[0] < 0
            or not isinstance(parsed[1], str)
            or not _MEDIA_ID_RE.fullmatch(parsed[1])
            or PlayerHttpServer._encode_favorite_cursor((parsed[1], parsed[0])) != value
        ):
            return None
        return parsed[0], parsed[1]

    async def _favorite(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        self._require_same_origin(request)
        media_id = request.match_info["media_id"]
        await self._media_details(media_id)
        await self._deck.favorite(digest, media_id)
        return web.json_response({"id": media_id, "favorite": True})

    async def _unfavorite(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        self._require_same_origin(request)
        media_id = request.match_info["media_id"]
        await self._deck.unfavorite(digest, media_id)
        return web.json_response({"id": media_id, "favorite": False})

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

        log_event(
            "media_delete_started",
            request_id=request_id,
            media=media_fingerprint,
            copies=len(locations),
        )

        deleted = 0
        failed = 0
        assert self._deleter is not None
        for copy_index, (package_id, package_path, remote_relpath) in enumerate(locations, start=1):
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
                media_id, package_id, remote_relpath
            )
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










    async def _authenticate(self, request: web.Request) -> str:
        token = request.cookies.get("tgvio_player_session")
        if not await self._sessions.authenticate(token):
            raise web.HTTPUnauthorized(text="authentication required")
        assert token is not None
        return token_digest(token)

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
        return {
            "id": media_id,
            "width": details["width"],
            "height": details["height"],
            "duration_seconds": duration,
            "size_bytes": details.get("size_bytes"),
            "stream_url": stream_url,
            "favorite": await self._repository.is_favorite(session_digest, media_id),
            "deletable": self._deleter is not None,
            "mime_type": details.get("mime_type"),
            "codec": details.get("codec"),
            "category": "long" if is_long else "short",
            "groups": groups,
        }

    @staticmethod
    async def _json_object(request: web.Request) -> dict[str, Any]:
        if request.content_length is not None and request.content_length > _MAX_JSON_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=_MAX_JSON_BYTES, actual_size=request.content_length)
        try:
            value = await request.json(loads=json.loads)
        except Exception:
            raise web.HTTPBadRequest(text="invalid JSON") from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return value

    @staticmethod
    def _require_same_origin(request: web.Request) -> None:
        origin = request.headers.get("Origin")
        if not origin:
            return
        # TLS usually terminates at the reverse proxy, so ``request.scheme`` can
        # be plain http even when the browser sent an https Origin. Compare the
        # origin host against Host / X-Forwarded-Host instead of an exact
        # scheme+host string; a cross-site attacker cannot forge those headers
        # from a browser.
        if origin == "null":
            raise web.HTTPForbidden(text="cross-origin request rejected")
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise web.HTTPForbidden(text="cross-origin request rejected")
        allowed = {request.host.lower()}
        forwarded_host = request.headers.get("X-Forwarded-Host")
        if forwarded_host:
            allowed.add(forwarded_host.split(",", 1)[0].strip().lower())
        if parsed.netloc.lower() not in allowed:
            raise web.HTTPForbidden(text="cross-origin request rejected")



    async def _login_allowed(self, client: str) -> bool:
        now = asyncio.get_running_loop().time()
        async with self._login_lock:
            state = self._login_failures.get(client)
            if state is None:
                return True
            _, window_started, locked_until = state
            if locked_until > now:
                return False
            if now - window_started >= _LOGIN_FAILURE_WINDOW_SECONDS:
                self._login_failures.pop(client, None)
            return True

    async def _record_login_failure(self, client: str) -> None:
        now = asyncio.get_running_loop().time()
        async with self._login_lock:
            failures, window_started, locked_until = self._login_failures.get(client, (0, now, 0.0))
            if now - window_started >= _LOGIN_FAILURE_WINDOW_SECONDS:
                failures, window_started = 0, now
            failures += 1
            if failures >= _LOGIN_FAILURE_LIMIT:
                locked_until = now + _LOGIN_LOCKOUT_SECONDS
                failures = 0
                window_started = now
            self._login_failures[client] = (failures, window_started, locked_until)

    async def _clear_login_failures(self, client: str) -> None:
        async with self._login_lock:
            self._login_failures.pop(client, None)
