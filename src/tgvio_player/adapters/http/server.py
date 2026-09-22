from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
import json
from pathlib import Path
from typing import Any

from aiohttp import web

from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.application.playback import StartupCacheKey, StartupRangeCache
from tgvio_player.application.streaming import prepare_stream_request
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
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; media-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


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
        self._max_header_size = max_header_size
        self._stream_chunk_size = stream_chunk_size
        self._startup_cache = startup_cache or StartupRangeCache(
            max_entries=_DEFAULT_STARTUP_CACHE_ENTRIES,
            max_bytes=_DEFAULT_STARTUP_CACHE_BYTES,
        )
        self._startup_range_bytes = startup_range_bytes
        self._static_dir = static_dir.resolve() if static_dir is not None and static_dir.is_dir() else None
        self._login_failures: dict[str, tuple[int, float, float]] = {}
        self._login_lock = asyncio.Lock()

    def application(self) -> web.Application:
        app = web.Application(client_max_size=_MAX_JSON_BYTES)
        app.on_response_prepare.append(self._security_headers)
        app.middlewares.append(self._request_limits)
        app.router.add_get("/healthz", self._healthz)
        app.router.add_post("/api/v1/auth/login", self._login)
        app.router.add_post("/api/v1/auth/logout", self._logout)
        app.router.add_get("/api/v1/feed", self._feed)
        app.router.add_get("/api/v1/media/{media_id}", self._media)
        app.router.add_get("/api/v1/media/{media_id}/stream", self._stream)
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
        del request
        response.headers.update(_SECURITY_HEADERS)

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
        client = request.remote or "unknown"
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
        media_ids = await self._deck.next_items(digest, limit=limit)
        items = []
        for media_id in media_ids:
            details = await self._repository.active_media_details(media_id)
            if details is not None:
                items.append(await self._media_dto(details, digest))
        return web.json_response({"items": items, "next_cursor": None})

    async def _media(self, request: web.Request) -> web.Response:
        digest = await self._authenticate(request)
        details = await self._media_details(request.match_info["media_id"])
        return web.json_response(await self._media_dto(details, digest))

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
        client = request.remote or "unknown"
        if not await self._acquire_stream(client):
            raise web.HTTPTooManyRequests(text="stream capacity reached")
        upstream: WebDavRangeResponse | None = None
        try:
            location = await self._repository.active_media_location(media_id)
            if location is None:
                raise web.HTTPNotFound(text="media not found")
            if self._is_startup_range(plan.byte_range):
                return await self._cached_startup_response(request, media_id, location, plan.byte_range, details)
            upstream = await self._reader.open_range(location[0], location[1], plan.byte_range)
            if upstream.status not in {200, 206}:
                raise web.HTTPBadGateway(text="media upstream unavailable")
            if plan.byte_range is not None and upstream.status != 206:
                raise web.HTTPBadGateway(text="media upstream ignored range")
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(upstream.content_length if upstream.content_length is not None else plan.content_length),
            }
            if upstream.content_type:
                headers["Content-Type"] = upstream.content_type
            if upstream.content_range:
                headers["Content-Range"] = upstream.content_range
            elif plan.content_range:
                headers["Content-Range"] = plan.content_range
            if upstream.etag:
                headers["ETag"] = upstream.etag
            response = web.StreamResponse(status=upstream.status, headers=headers)
            await response.prepare(request)
            async for chunk in upstream.body:
                if chunk:
                    await response.write(chunk[: self._stream_chunk_size])
                    remainder = chunk[self._stream_chunk_size :]
                    while remainder:
                        await response.write(remainder[: self._stream_chunk_size])
                        remainder = remainder[self._stream_chunk_size :]
            await response.write_eof()
            return response
        finally:
            if upstream is not None:
                await self._close_body(upstream.body)
            await self._release_stream(client)

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

    async def _media_dto(self, details: dict[str, object], session_digest: str) -> dict[str, object]:
        media_id = str(details["media_id"])
        return {
            "id": media_id,
            "width": details["width"],
            "height": details["height"],
            "duration_seconds": details["duration_seconds"],
            "stream_url": f"/api/v1/media/{media_id}/stream",
            "favorite": await self._repository.is_favorite(session_digest, media_id),
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
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="cross-origin request rejected")

    async def _acquire_stream(self, client: str) -> bool:
        async with self._stream_lock:
            if self._stream_clients[client] >= self._max_streams_per_client or self._stream_slots.locked():
                return False
            await self._stream_slots.acquire()
            self._stream_clients[client] += 1
            return True

    async def _release_stream(self, client: str) -> None:
        async with self._stream_lock:
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
