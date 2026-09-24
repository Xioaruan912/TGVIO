from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from aiohttp import web

from tgvio_player.application.playback import StartupCacheKey
from tgvio_player.application.streaming import StreamRequest, prepare_stream_request
from tgvio_player.domain.ranges import ByteRange, RangeNotSatisfiable
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse

from .client import _prefetch_requested, resolve_client
from .diagnostics import client_fingerprint, fingerprint, log_event


_FASTSTART_WAIT_SECONDS = 2.0
_FOREGROUND_STREAM_WAIT_SECONDS = 3.0
_STREAM_SLOT_POLL_SECONDS = 0.025
_PRELOAD_HEADER = "X-TGVIO-Preload"


class PlayerHttpStreamingMixin:
    """Player media streaming and fair playback-slot coordination."""

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
        capacity = {}
        if not await self._acquire_stream(client, preload=preload, diagnostics=capacity):
            log_event(
                "stream_rejected",
                request_id=request.get("player_request_id"),
                media=fingerprint(media_id),
                client=client_fingerprint(client),
                mode="preload" if preload else "foreground",
                reason=capacity.get("reason", "capacity_timeout"),
                wait_ms=capacity.get("wait_ms", 0),
                active_playback=self.active_playback_streams,
                active_preload=self._preload_active,
                foreground_waiters=self._foreground_waiters,
                global_limit=self._max_streams,
                client_limit=self._max_streams_per_client,
            )
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

    async def _acquire_stream(
        self,
        client: str,
        *,
        preload: bool = False,
        diagnostics: dict[str, object] | None = None,
    ) -> bool:
        started = asyncio.get_running_loop().time()
        if preload:
            async with self._stream_lock:
                # Speculative warm-ups never count against playback and are the
                # first thing dropped when global capacity is tight.
                if (
                    self._foreground_waiters
                    or self._preload_active >= self._max_preload
                    or self._stream_slots.locked()
                ):
                    if diagnostics is not None:
                        diagnostics.update(
                            reason=(
                                "foreground_waiting"
                                if self._foreground_waiters
                                else "preload_limit"
                                if self._preload_active >= self._max_preload
                                else "global_capacity"
                            ),
                            wait_ms=0,
                        )
                    return False
                await self._stream_slots.acquire()
                self._preload_active += 1
                return True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _FOREGROUND_STREAM_WAIT_SECONDS
        async with self._stream_lock:
            self._foreground_waiters += 1
        try:
            while loop.time() < deadline:
                async with self._stream_lock:
                    client_has_capacity = (
                        self._stream_clients[client] < self._max_streams_per_client
                    )
                    if client_has_capacity and not self._stream_slots.locked():
                        await self._stream_slots.acquire()
                        self._stream_clients[client] += 1
                        return True
                await asyncio.sleep(_STREAM_SLOT_POLL_SECONDS)
            if diagnostics is not None:
                diagnostics.update(
                    reason=(
                        "client_limit"
                        if self._stream_clients[client] >= self._max_streams_per_client
                        else "global_capacity_timeout"
                    ),
                    wait_ms=round((loop.time() - started) * 1000),
                )
            return False
        finally:
            async with self._stream_lock:
                self._foreground_waiters = max(0, self._foreground_waiters - 1)

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

    @staticmethod
    async def _close_body(body: AsyncIterator[bytes]) -> None:
        close = getattr(body, "aclose", None)
        if close is not None:
            await close()
