from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.application.playback import StartupCacheKey, StartupRangeCache
from tgvio_player.application.streaming import StreamRequest, prepare_stream_request
from tgvio_player.domain.auth import token_digest
from tgvio_player.domain.ranges import ByteRange, RangeNotSatisfiable
from tgvio_player.infrastructure.webdav_read import ReadOnlyWebDavAdapter, WebDavRangeResponse


_MAX_JSON_BYTES = 4096
_MAX_FEED_LIMIT = 20
_DEFAULT_STARTUP_CACHE_ENTRIES = 32
_DEFAULT_STARTUP_CACHE_BYTES = 64 * 1024 * 1024
_DEFAULT_STARTUP_RANGE_BYTES = 2 * 1024 * 1024
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_FAILURE_WINDOW_SECONDS = 10 * 60
_LOGIN_LOCKOUT_SECONDS = 15 * 60
_FASTSTART_WAIT_SECONDS = 6.0
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; media-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_PRELOAD_HEADER = "X-TGVIO-Preload"
_TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_trusted_proxy(host: str) -> bool:
    candidate = (host or "").strip()
    if not candidate:
        return False
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return candidate in {"localhost"}
    return any(address in network for network in _TRUSTED_PROXY_NETWORKS)


def resolve_client(request: web.Request) -> str:
    """Return the real client identity behind the local reverse proxy.

    The Player binds loopback only and is always reached through nginx, so
    ``request.remote`` is the proxy address for every visitor. When (and only
    when) the immediate peer is a trusted private/loopback address we take the
    proxy-appended address: ``X-Real-IP`` if present, otherwise the right-most
    ``X-Forwarded-For`` hop (the one nginx itself added, which a browser cannot
    forge). Direct peers keep their own address.
    """
    peer = request.remote or "unknown"
    if not _is_trusted_proxy(peer):
        return peer
    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    if real_ip:
        return real_ip
    forwarded = request.headers.get("X-Forwarded-For", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if hops:
        return hops[-1]
    return peer


def _prefetch_requested(request: web.Request) -> bool:
    cache = (request.query.get("cache") or "").strip().lower()
    prefetch = (request.query.get("prefetch") or "").strip().lower()
    return cache in {"1", "true", "yes"} or prefetch in {"1", "true", "yes"}


class PlayerHttpServer:
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
    ) -> None:
        if min(
            max_streams, max_streams_per_client, max_header_size, stream_chunk_size, startup_range_bytes
        ) < 1:
            raise ValueError("HTTP limits must be positive")
        self._repository = repository
        self._sessions = sessions
        self._deck = deck
        self._reader = reader
        self._stream_slots = asyncio.BoundedSemaphore(max_streams)
        self._max_streams_per_client = max_streams_per_client
        self._stream_clients: Counter[str] = Counter()
        self._stream_lock = asyncio.Lock()
        # Speculative preloads must never consume the playback budget. They use
        # a small bounded share of the global slots and are dropped first.
        self._max_preload = max(1, max_streams // 4)
        self._preload_active = 0
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
        self._login_failures: dict[str, tuple[int, float, float]] = {}
        self._login_lock = asyncio.Lock()

    @property
    def active_playback_streams(self) -> int:
        """Global playback stream count, used to pause low-priority work."""
        return sum(self._stream_clients.values())

    def application(self) -> web.Application:
        app = web.Application(client_max_size=_MAX_JSON_BYTES)
        app.on_response_prepare.append(self._security_headers)
        app.middlewares.append(self._request_limits)
        app.router.add_get("/healthz", self._healthz)
        app.router.add_post("/api/v1/auth/login", self._login)
        app.router.add_post("/api/v1/auth/logout", self._logout)
        app.router.add_get("/api/v1/feed", self._feed)
        app.router.add_get("/api/v1/videos", self._videos)
        app.router.add_get("/api/v1/favorites", self._favorites)
        app.router.add_get("/api/v1/media/{media_id}", self._media)
        app.router.add_get("/api/v1/media/{media_id}/stream", self._stream)
        app.router.add_post("/api/v1/media/{media_id}/prepare", self._prepare)
        app.router.add_put("/api/v1/media/{media_id}/favorite", self._favorite)
        app.router.add_delete("/api/v1/media/{media_id}/favorite", self._unfavorite)
        if self._static_dir is not None:
            app.router.add_get("/", self._frontend_index)
            assets = self._static_dir / "assets"
            if assets.is_dir():
                app.router.add_static("/assets", assets, show_index=False, follow_symlinks=False)
        return app

    def runner(self) -> web.AppRunner:
        """Create a runner with parser-level request-line and header bounds."""
        return web.AppRunner(
            self.application(),
            max_field_size=self._max_header_size,
            max_line_size=self._max_header_size,
        )

    @web.middleware
    async def _request_limits(self, request: web.Request, handler: Any) -> web.StreamResponse:
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
        return await handler(request)

    async def _security_headers(self, request: web.Request, response: web.StreamResponse) -> None:
        response.headers.update(_SECURITY_HEADERS)
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

    async def _frontend_index(self, request: web.Request) -> web.FileResponse:
        del request
        assert self._static_dir is not None
        index = self._static_dir / "index.html"
        if not index.is_file():
            raise web.HTTPNotFound(text="frontend unavailable")
        return web.FileResponse(index)

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
                items.append(await self._media_dto(details, digest, prefetch=prefetch))
        return web.json_response({"items": items, "next_cursor": None})

    async def _videos(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        category = request.query.get("category", "short")
        if category not in {"short", "long"}:
            raise web.HTTPBadRequest(text="invalid category")
        try:
            limit = int(request.query.get("limit", "20"))
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid paging") from None
        if not 1 <= limit <= _MAX_FEED_LIMIT or offset < 0:
            raise web.HTTPBadRequest(text="invalid paging")
        if category == "long":
            media_ids = await self._repository.list_video_ids(
                min_seconds=self._large_video_seconds,
                order="duration_desc",
                limit=limit + 1,
                offset=offset,
            )
        else:
            media_ids = await self._repository.list_video_ids(
                max_seconds=self._large_video_seconds,
                order="media_id",
                limit=limit + 1,
                offset=offset,
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
                items.append(await self._media_dto(details, digest, prefetch=prefetch))
        return web.json_response({"items": items, "has_more": has_more, "category": category})

    async def _media(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        details = await self._media_details(request.match_info["media_id"])
        return web.json_response(
            await self._media_dto(details, digest, prefetch=_prefetch_requested(request))
        )

    async def _favorites(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        prefetch = _prefetch_requested(request)
        media_ids = await self._deck.list_favorites(digest, limit=200)
        items = []
        for media_id in media_ids:
            details = await self._repository.active_media_details(media_id)
            if details is not None:
                items.append(await self._media_dto(details, digest, prefetch=prefetch))
        return web.json_response({"items": items, "next_cursor": None})

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

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        await self._authenticate(request)
        media_id = request.match_info["media_id"]
        details = await self._media_details(media_id)
        try:
            plan = prepare_stream_request(request.headers.get("Range"), size_bytes=int(details["size_bytes"]))
        except RangeNotSatisfiable:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{details['size_bytes']}"})
        # Try to build the faststart overlay before taking a stream slot, so the
        # build never holds playback capacity. If it is not ready within a short
        # budget we fall back to the original file and build in the background.
        overlay = None
        if self._faststart is not None:
            overlay = self._faststart.peek(media_id, details)
            if overlay is None:
                try:
                    overlay = await asyncio.wait_for(
                        self._faststart.overlay_for(media_id, details, priority=True),
                        timeout=_FASTSTART_WAIT_SECONDS,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    overlay = None
                except Exception:
                    overlay = None
                if overlay is None:
                    self._faststart.schedule(media_id, details)
        client = resolve_client(request)
        preload = request.headers.get(_PRELOAD_HEADER) == "1"
        if not await self._acquire_stream(client, preload=preload):
            raise web.HTTPTooManyRequests(text="stream capacity reached")
        try:
            location = await self._repository.active_media_location(media_id)
            if location is None:
                raise web.HTTPNotFound(text="media not found")
            prefetch = _prefetch_requested(request) and not preload
            if overlay is not None:
                return await self._stream_overlay(request, media_id, overlay, location, plan, prefetch)
            if self._is_startup_range(plan.byte_range):
                return await self._cached_startup_response(request, media_id, location, plan.byte_range, details)
            return await self._stream_plain(request, media_id, location, details, plan, prefetch)
        finally:
            await self._release_stream(client, preload=preload)

    async def _stream_plain(
        self,
        request: web.Request,
        media_id: str,
        location: tuple[str, str, str | None],
        details: dict[str, object],
        plan: StreamRequest,
        prefetch: bool,
    ) -> web.StreamResponse:
        size = int(details["size_bytes"])
        if plan.byte_range is None:
            start, end, status = 0, size - 1, 200
        else:
            start, end, status = plan.byte_range.start, plan.byte_range.end, 206
        mime = details.get("mime_type")
        content_type = (
            str(mime)
            if isinstance(mime, str) and mime and mime != "application/octet-stream"
            else "video/mp4"
        )
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": content_type,
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        data = await self._open_data(
            media_id, location, size, ByteRange(start, end), prefetch
        )
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        if data is not None:
            async for chunk in data:
                if chunk:
                    await self._write_chunks(response, chunk)
        await response.write_eof()
        return response

    async def _open_data(
        self,
        media_id: str,
        location: tuple[str, str, str | None],
        size: int,
        byte_range: ByteRange,
        prefetch: bool,
    ):
        """Open a validated data stream, fetching the first chunk up front.

        Validating before the response is prepared lets a bad upstream become a
        clean 502 instead of a truncated 200/206 body.
        """
        if self._range_cache is not None:
            try:
                await self._range_cache.prime(
                    media_id, location[0], location[1], size, byte_range
                )
            except Exception as exc:
                raise web.HTTPBadGateway(text="media upstream unavailable") from exc
            return self._range_cache.stream(
                media_id, location[0], location[1], size, byte_range, prefetch=prefetch
            )
        upstream = await self._reader.open_range(location[0], location[1], byte_range)
        if upstream.status not in {200, 206}:
            await self._close_body(upstream.body)
            raise web.HTTPBadGateway(text="media upstream unavailable")

        async def upstream_stream():
            try:
                async for chunk in upstream.body:
                    if chunk:
                        yield chunk
            finally:
                await self._close_body(upstream.body)

        return upstream_stream()

    async def _stream_overlay(
        self,
        request: web.Request,
        media_id: str,
        overlay: Any,
        location: tuple[str, str, str | None],
        plan: StreamRequest,
        prefetch: bool,
    ) -> web.StreamResponse:
        """Serve a virtual faststart view: cached ``moov`` first, then remote data."""
        size = int(overlay.size)
        if plan.byte_range is None:
            start, end, status = 0, size - 1, 200
        else:
            start, end, status = plan.byte_range.start, plan.byte_range.end, 206
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": str(overlay.mime),
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        segments = overlay.segments(start, end)
        data = None
        for kind, source_offset, length in segments:
            if kind != "cache" and length > 0:
                data = await self._open_data(
                    media_id,
                    location,
                    size,
                    ByteRange(source_offset, source_offset + length - 1),
                    prefetch,
                )
                break
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        for kind, source_offset, length in segments:
            if length <= 0:
                continue
            if kind == "cache":
                await self._write_chunks(response, overlay.head[source_offset : source_offset + length])
        if data is not None:
            async for chunk in data:
                if chunk:
                    await self._write_chunks(response, chunk)
        await response.write_eof()
        return response

    async def _write_chunks(self, response: web.StreamResponse, data: bytes) -> None:
        size = self._stream_chunk_size
        for offset in range(0, len(data), size):
            await response.write(data[offset : offset + size])

    async def _prepare(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        if self._faststart is None:
            return web.json_response({"prepared": False}, status=202)
        media_id = request.match_info["media_id"]
        details = await self._media_details(media_id)
        self._faststart.schedule(media_id, details)
        return web.json_response({"prepared": True}, status=202)

    def _is_startup_range(self, byte_range: ByteRange | None) -> bool:
        return (
            byte_range is not None
            and byte_range.start == 0
            and byte_range.length <= self._startup_range_bytes
        )

    async def _cached_startup_response(
        self,
        request: web.Request,
        media_id: str,
        location: tuple[str, str, str | None],
        byte_range: ByteRange,
        details: dict[str, object],
    ) -> web.StreamResponse:
        key = StartupCacheKey(media_id, location[2], byte_range)
        cached, task = await self._startup_cache.acquire(
            key,
            lambda: self._fetch_startup_range(location, byte_range),
        )
        if cached is None:
            assert task is not None
            try:
                # The lease, rather than one HTTP handler, owns the shared task.
                # A disconnected consumer therefore cannot cancel other waiters.
                cached = await asyncio.shield(task)
            finally:
                await self._startup_cache.release(key)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(cached)),
            "Content-Range": f"bytes {byte_range.start}-{byte_range.end}/{details['size_bytes']}",
        }
        mime_type = details.get("mime_type")
        if isinstance(mime_type, str) and mime_type:
            headers["Content-Type"] = mime_type
        if location[2]:
            headers["ETag"] = location[2]
        response = web.StreamResponse(status=206, headers=headers)
        await response.prepare(request)
        for offset in range(0, len(cached), self._stream_chunk_size):
            await response.write(cached[offset : offset + self._stream_chunk_size])
        await response.write_eof()
        return response

    async def _fetch_startup_range(
        self,
        location: tuple[str, str, str | None],
        byte_range: ByteRange,
    ) -> bytes:
        upstream: WebDavRangeResponse | None = None
        try:
            upstream = await self._reader.open_range(location[0], location[1], byte_range)
            if upstream.status != 206:
                raise RuntimeError("media upstream ignored startup range")
            if upstream.content_length is not None and upstream.content_length != byte_range.length:
                raise RuntimeError("media upstream returned invalid startup range length")
            payload = bytearray()
            async for chunk in upstream.body:
                remaining = byte_range.length - len(payload)
                if remaining <= 0:
                    raise RuntimeError("media upstream exceeded startup range")
                if len(chunk) > remaining:
                    raise RuntimeError("media upstream exceeded startup range")
                payload.extend(chunk)
            if len(payload) != byte_range.length:
                raise RuntimeError("media upstream returned incomplete startup range")
            return bytes(payload)
        finally:
            if upstream is not None:
                await self._close_body(upstream.body)

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
        return {
            "id": media_id,
            "width": details["width"],
            "height": details["height"],
            "duration_seconds": duration,
            "size_bytes": details.get("size_bytes"),
            "stream_url": stream_url,
            "favorite": await self._repository.is_favorite(session_digest, media_id),
            "mime_type": details.get("mime_type"),
            "codec": details.get("codec"),
            "category": "long" if is_long else "short",
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

    async def _acquire_stream(self, client: str, *, preload: bool = False) -> bool:
        async with self._stream_lock:
            if preload:
                # Speculative warm-ups never count against playback and are the
                # first thing dropped when global capacity is tight.
                if self._preload_active >= self._max_preload or self._stream_slots.locked():
                    return False
                await self._stream_slots.acquire()
                self._preload_active += 1
                return True
            if self._stream_clients[client] >= self._max_streams_per_client or self._stream_slots.locked():
                return False
            await self._stream_slots.acquire()
            self._stream_clients[client] += 1
            return True

    async def _release_stream(self, client: str, *, preload: bool = False) -> None:
        async with self._stream_lock:
            if preload:
                if self._preload_active > 0:
                    self._preload_active -= 1
                    self._stream_slots.release()
                return
            if self._stream_clients[client] > 0:
                self._stream_clients[client] -= 1
                self._stream_slots.release()
            if not self._stream_clients[client]:
                self._stream_clients.pop(client, None)

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

    @staticmethod
    async def _close_body(body: AsyncIterator[bytes]) -> None:
        close = getattr(body, "aclose", None)
        if close is not None:
            await close()
