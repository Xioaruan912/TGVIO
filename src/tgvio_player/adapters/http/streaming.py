from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from aiohttp import web

from tgvio_player.application.playback import StartupCacheKey
from tgvio_player.application.streaming import StreamRequest, prepare_stream_request
from tgvio_player.domain.catalog import MAX_COVER_BYTES
from tgvio_player.domain.ranges import ByteRange, RangeNotSatisfiable
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse

from .client import _prefetch_requested, resolve_client
from .diagnostics import client_fingerprint, fingerprint, log_event


_FASTSTART_WAIT_SECONDS = 2.0
_FOREGROUND_STREAM_WAIT_SECONDS = 3.0
_STREAM_SLOT_POLL_SECONDS = 0.025
_PRELOAD_HEADER = "X-TGVIO-Preload"
# A cover is a small still declared by the catalog; the cap here is the same one
# the archive writer enforces, so a malformed manifest can never make the Player
# read an unbounded amount of upstream data for a thumbnail.
_MAX_COVER_BYTES = MAX_COVER_BYTES
# Chromium (and several other players) ask for two bytes to discover range
# support before real playback. Such a probe is a capability check, not playback.
_CAPABILITY_PROBE_BYTES = 2
_DOWNLOAD_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})
# Only whitelisted media types may contribute a filename extension. Anything
# unrecognised falls back to ``.bin`` rather than echoing a client value.
_DOWNLOAD_EXTENSIONS: dict[str, str] = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpeg",
    "video/3gpp": ".3gp",
    "video/ogg": ".ogv",
    "video/mp2t": ".ts",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


def _download_requested(request: web.Request) -> bool:
    """``?download=1`` switches a stream from inline playback to a save."""
    value = request.query.get("download")
    return isinstance(value, str) and value.strip().lower() in _DOWNLOAD_FLAG_VALUES


def _tail_requested(request: web.Request) -> bool:
    """``?tail=1`` asks the server to warm the end of a clip for a seek."""
    value = request.query.get("tail")
    return isinstance(value, str) and value.strip().lower() in _DOWNLOAD_FLAG_VALUES


def _media_response_headers(
    request: web.Request,
    media_id: str,
    size: int,
    mime_type: object,
    plan: StreamRequest,
) -> tuple[int, int, int, dict[str, str]]:
    """Build the shared status/range header set for playback, download and HEAD.

    Keeping one builder means a HEAD advertises exactly what the body response
    would, so a client that probes with HEAD and then ranges does not see the
    two disagree.
    """
    if plan.byte_range is None:
        start, end, status = 0, size - 1, 200
    else:
        start, end, status = plan.byte_range.start, plan.byte_range.end, 206
    content_type = (
        str(mime_type)
        if isinstance(mime_type, str) and mime_type and mime_type != "application/octet-stream"
        else "video/mp4"
    )
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type": content_type,
        "Content-Disposition": _content_disposition(request, media_id, content_type),
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return status, start, end, headers


def _content_disposition(request: web.Request, media_id: str, mime_type: object) -> str:
    """Build a safe ``Content-Disposition`` for playback or download.

    The filename is the media fingerprint plus a whitelisted extension, so no
    remote path, owner data or user-supplied text can reach the header, and a
    browser's "save as" gets a meaningful name instead of the route name.
    """
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    extension = _DOWNLOAD_EXTENSIONS.get(mime, ".bin")
    disposition = "attachment" if _download_requested(request) else "inline"
    return f'{disposition}; filename="{fingerprint(media_id)}{extension}"'


class StartupRangeUnavailable(RuntimeError):
    def __init__(self, reason: str, upstream_status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.upstream_status = upstream_status


class IncompleteUpstreamBody(RuntimeError):
    """The upstream ended before delivering the range we already declared.

    An explicit ``Content-Length`` disables aiohttp's own length check, so ending
    the response cleanly here leaves a body that never arrives: the player stalls
    with no error and never reaches its retry path.
    """


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
        if request.method == "HEAD":
            # A HEAD must stay free: no archive read, no stream slot, no faststart
            # build. Players and proxies use it to learn the size and range
            # support before deciding how to fetch.
            status, _, _, headers = _media_response_headers(
                request, media_id, int(details["size_bytes"]), details.get("mime_type"), plan
            )
            return web.Response(status=status, headers=headers)
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
        probe = (
            not preload
            and plan.byte_range is not None
            and plan.byte_range.start == 0
            and plan.byte_range.length <= _CAPABILITY_PROBE_BYTES
        )
        mode = "probe" if probe else "preload" if preload else "foreground"
        capacity: dict[str, object] = {}
        admitted = (
            await self._acquire_probe(diagnostics=capacity)
            if probe
            else await self._acquire_stream(client, preload=preload, diagnostics=capacity)
        )
        if not admitted:
            log_event(
                "stream_rejected",
                request_id=request.get("player_request_id"),
                media=fingerprint(media_id),
                client=client_fingerprint(client),
                mode=mode,
                reason=capacity.get("reason", "capacity_timeout"),
                wait_ms=capacity.get("wait_ms", 0),
                active_playback=self.active_playback_streams,
                active_preload=self._preload_active,
                active_probe=self._probe_active,
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
                try:
                    return await self._cached_startup_response(request, media_id, location, plan.byte_range, details)
                except StartupRangeUnavailable as exc:
                    log_event(
                        "preload_skipped" if preload else "startup_range_fallback",
                        request_id=request.get("player_request_id"),
                        media=fingerprint(media_id),
                        media_id=media_id,
                        range=request.headers.get("Range"),
                        upstream_status=exc.upstream_status,
                        reason=exc.reason,
                    )
                    if preload:
                        return web.Response(
                            status=204,
                            headers={
                                "X-TGVIO-Preload-Outcome": "skipped",
                                "X-TGVIO-Preload-Reason": exc.reason,
                            },
                        )
                    return await self._stream_plain(request, media_id, location, details, plan, prefetch)
            return await self._stream_plain(request, media_id, location, details, plan, prefetch)
        finally:
            if probe:
                await self._release_probe()
            else:
                await self._release_stream(client, preload=preload)

    async def _cover(self, request: web.Request) -> web.StreamResponse:
        """Serve one small, private still from its own budget.

        A cover is decorative: it never takes a playback slot, never waits for
        capacity, and is bounded by what the catalog declared. Whatever the
        upstream does, the reader gets either the whole declared image or an
        explicit failure - never a half image that looks like a broken video.
        """
        await self._authenticate(request)
        media_id = request.match_info["media_id"]
        await self._media_details(media_id)
        cover = await self._repository.active_cover(media_id)
        if cover is None:
            raise web.HTTPNotFound(text="cover not found")
        version = request.query.get("v")
        if version is not None and version != cover["version"]:
            raise web.HTTPNotFound(text="cover not found")
        try:
            declared = int(cover["size_bytes"])
        except (TypeError, ValueError):
            declared = 0
        length = min(declared, _MAX_COVER_BYTES)
        if length <= 0:
            raise web.HTTPNotFound(text="cover not found")
        # The mirror answers before the budget: a local copy is not an archive round trip,
        # so it neither needs a cover slot nor counts as one.
        mirror, counters = self._cover_mirror, self._cover_mirror_counters
        key = mirror.key_for(str(cover["remote_relpath"])) if mirror is not None else None
        if mirror is not None and key is not None and mirror.has(key, length):
            payload = await asyncio.to_thread(mirror.read, key)
            if payload is not None:
                if counters is not None:
                    counters.hits += 1
                return await self._serve_cover_bytes(request, media_id, cover, payload, version)
        if mirror is not None and counters is not None:
            counters.misses += 1
        capacity: dict[str, object] = {}
        if not await self._acquire_cover(diagnostics=capacity):
            log_event(
                "cover_rejected",
                request_id=request.get("player_request_id"),
                media=fingerprint(media_id),
                reason=capacity.get("reason", "cover_limit"),
                active_cover=self._cover_active,
                cover_limit=self._max_cover,
            )
            raise web.HTTPServiceUnavailable(text="cover capacity reached")
        try:
            try:
                upstream = await self._reader.open_range(
                    str(cover["remote_path"]), str(cover["remote_relpath"]), ByteRange(0, length - 1)
                )
            except Exception as exc:
                # The archive is unreachable but a local copy exists: answer with it rather
                # than failing a request the host can already satisfy.
                if mirror is not None and key is not None and mirror.exists(key):
                    payload = await asyncio.to_thread(mirror.read, key)
                    if payload is not None:
                        return await self._serve_cover_bytes(request, media_id, cover, payload, version)
                raise web.HTTPBadGateway(text="cover upstream unavailable") from exc
            # Own the upstream before status validation or response preparation.
            # Even an unread error body or a cancelled prepare must be closed.
            try:
                if upstream.status not in {200, 206}:
                    if upstream.status == 404:
                        raise web.HTTPNotFound(text="cover not found")
                    raise web.HTTPBadGateway(text="cover upstream unavailable")
                response = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": str(cover["mime_type"]),
                        "Content-Length": str(length),
                        "Content-Disposition": _content_disposition(request, media_id, cover["mime_type"]),
                    },
                )
                request["player_cover_cacheable"] = version is not None
                await response.prepare(request)
                written = 0
                collected = bytearray() if key is not None else None
                try:
                    async for chunk in upstream.body:
                        if not chunk:
                            continue
                        piece = chunk[: length - written]
                        written += len(piece)
                        if collected is not None:
                            collected.extend(piece)
                        await response.write(piece)
                        if written >= length:
                            break  # Never drain an ignored Range or infinite tail.
                    if written < length:
                        request["player_cover_incomplete"] = f"{written}/{length}"
                        raise IncompleteUpstreamBody(f"{written} of {length} cover bytes")
                    if collected is not None and mirror is not None and key is not None:
                        await self._fill_mirror(mirror, counters, key, bytes(collected))
                    await response.write_eof()
                except (BrokenPipeError, ConnectionError, ConnectionResetError) as exc:
                    request["player_stream_disconnect"] = type(exc).__name__
                return response
            finally:
                await self._close_body(upstream.body)
        finally:
            await self._release_cover()

    async def _serve_cover_bytes(
        self,
        request: web.Request,
        media_id: str,
        cover: dict[str, object],
        payload: bytes,
        version: str | None,
    ) -> web.StreamResponse:
        """The three headers a cover always carries, for bytes that are already in hand."""
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": str(cover["mime_type"]),
                "Content-Length": str(len(payload)),
                "Content-Disposition": _content_disposition(request, media_id, cover["mime_type"]),
            },
        )
        request["player_cover_cacheable"] = version is not None
        await response.prepare(request)
        try:
            await response.write(payload)
            await response.write_eof()
        except (BrokenPipeError, ConnectionError, ConnectionResetError) as exc:
            request["player_stream_disconnect"] = type(exc).__name__
        return response

    async def _fill_mirror(
        self, mirror: object, counters: object | None, key: str, payload: bytes
    ) -> None:
        """Best effort: a cover that reached the reader must not fail because the cache did."""
        try:
            await asyncio.to_thread(mirror.write, key, payload)  # type: ignore[attr-defined]
        except Exception:
            if counters is not None:
                counters.write_failed += 1  # type: ignore[attr-defined]
            log_event("cover_mirror_write_failed", key=key[:12])

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
        status, start, end, headers = _media_response_headers(
            request, media_id, size, details.get("mime_type"), plan
        )
        data = await self._open_data(
            media_id, location, size, ByteRange(start, end), prefetch
        )
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        expected = int(headers["Content-Length"])
        written = 0
        try:
            if data is not None:
                async for chunk in data:
                    if chunk:
                        written += len(chunk)
                        await self._write_chunks(response, chunk)
            if written < expected:
                request["player_stream_incomplete"] = f"{written}/{expected}"
                raise IncompleteUpstreamBody(f"{written} of {expected} bytes")
            await response.write_eof()
        except (BrokenPipeError, ConnectionError, ConnectionResetError) as exc:
            request["player_stream_disconnect"] = type(exc).__name__
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
            "Content-Disposition": _content_disposition(request, media_id, str(overlay.mime)),
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
        try:
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
        except (BrokenPipeError, ConnectionError, ConnectionResetError) as exc:
            request["player_stream_disconnect"] = type(exc).__name__
        return response

    async def _write_chunks(self, response: web.StreamResponse, data: bytes) -> None:
        size = self._stream_chunk_size
        for offset in range(0, len(data), size):
            await response.write(data[offset : offset + size])

    async def _prepare(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        media_id = request.match_info["media_id"]
        details = await self._media_details(media_id)
        prepared = False
        if self._faststart is not None:
            self._faststart.schedule(media_id, details)
            prepared = True
        if _tail_requested(request):
            prepared = await self._warm_tail(media_id, details) or prepared
        return web.json_response({"prepared": prepared}, status=202)

    async def _warm_tail(self, media_id: str, details: dict[str, object]) -> bool:
        """Warm the tail window for a viewer who is dragging towards the end.

        This is the cost-bounded half of tail caching: the client asks for the
        clip it is actually about to seek in, instead of the server warming the
        tail of every catalog entry.
        """
        cache = self._range_cache
        if cache is None or self._warm_tail_bytes <= 0:
            return False
        descriptor = getattr(cache, "prefetch_tail", None)
        if descriptor is None:
            return False
        location = await self._repository.active_media_location(media_id)
        size = int(details.get("size_bytes") or 0)
        if location is None or size <= 0:
            return False
        descriptor(media_id, location[0], location[1], size, min(self._warm_tail_bytes, size))
        return True

    async def _schedule_prefetch(
        self, media_id: str, details: dict[str, object]
    ) -> None:
        """Warm the ends of a clip the client is about to play.

        Runs off the feed listing, so the first frame and a seek to the end are
        both served locally without the viewer having to drag first.
        """
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
        # Long clips are the ones a viewer scrubs to the end of, and the last
        # window is expensive on a slow archive; short clips play through their
        # tail anyway, so only long ones are warmed here.
        tail = getattr(self._range_cache, "prefetch_tail", None)
        duration = details.get("duration_seconds")
        if (
            tail is not None
            and self._warm_tail_bytes > 0
            and isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and float(duration) >= self._large_video_seconds
            and size > self._warm_tail_bytes
        ):
            tail(media_id, location[0], location[1], size, min(self._warm_tail_bytes, size))

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
            "Content-Disposition": _content_disposition(
                request, media_id, details.get("mime_type") or "video/mp4"
            ),
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
                raise StartupRangeUnavailable("range_ignored", upstream.status)
            if upstream.content_length is not None and upstream.content_length != byte_range.length:
                raise StartupRangeUnavailable("range_length_invalid", upstream.status)
            payload = bytearray()
            async for chunk in upstream.body:
                remaining = byte_range.length - len(payload)
                if remaining <= 0:
                    raise StartupRangeUnavailable("range_exceeded", upstream.status)
                if len(chunk) > remaining:
                    raise StartupRangeUnavailable("range_exceeded", upstream.status)
                payload.extend(chunk)
            if len(payload) != byte_range.length:
                raise StartupRangeUnavailable("range_incomplete", upstream.status)
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
                    or (
                        self.active_playback_streams + self._preload_active
                        >= self._max_streams - 1
                    )
                    or self._stream_slots.locked()
                ):
                    if diagnostics is not None:
                        diagnostics.update(
                            reason=(
                                "foreground_waiting"
                                if self._foreground_waiters
                                else "preload_limit"
                                if self._preload_active >= self._max_preload
                                else "playback_capacity_reserved"
                                if self.active_playback_streams + self._preload_active
                                >= self._max_streams - 1
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

    async def _acquire_probe(self, *, diagnostics: dict[str, object] | None = None) -> bool:
        """Capability probes use their own tiny budget, never a playback slot."""
        async with self._stream_lock:
            if self._probe_active >= self._max_probe:
                if diagnostics is not None:
                    diagnostics.update(reason="probe_limit", wait_ms=0)
                return False
            self._probe_active += 1
            return True

    async def _release_probe(self) -> None:
        async with self._stream_lock:
            if self._probe_active > 0:
                self._probe_active -= 1

    async def _acquire_cover(self, *, diagnostics: dict[str, object] | None = None) -> bool:
        """Covers are separate, tiny and fail fast: browsing never waits on playback."""
        async with self._stream_lock:
            if self._cover_active >= self._max_cover:
                if diagnostics is not None:
                    diagnostics.update(reason="cover_limit", wait_ms=0)
                return False
            self._cover_active += 1
            return True

    async def _release_cover(self) -> None:
        async with self._stream_lock:
            if self._cover_active > 0:
                self._cover_active -= 1

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
