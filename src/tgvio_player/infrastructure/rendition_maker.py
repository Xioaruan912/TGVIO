"""480p copies of plain-folder videos, made by the player itself.

The owner often watches the low-res copy: it caches and starts faster. ffmpeg
reads the original through a loopback-only endpoint served by this process, so
range reads go through the player's own reader (which renews cloud-drive links)
and no OpenList credential is handed to ffmpeg. The copy is uploaded to
``<library>/.tgvio/<media_id>/480.mp4``, where the catalog scan links it to the
original. Work starts only when nothing plays, ffmpeg runs at the lowest CPU
priority, and it is paused (SIGSTOP) whenever playback begins.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import math
from pathlib import Path
import secrets
import signal

import aiohttp
from aiohttp import web

from tgvio_player.domain.ranges import ByteRange
from tgvio_player.infrastructure.folder_catalog import COVER_FILE, RENDITIONS_DIR

_LOG = logging.getLogger(__name__)
HEIGHT = 480
_CHUNK = 1024 * 1024


async def _run(*args: str, timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
        raise
    if proc.returncode:
        raise RuntimeError("media command failed")
    return out.decode("utf-8")


def ffmpeg_args(source_url: str, target: Path) -> list[str]:
    # Same encode as the maintenance container's copies, so both kinds look alike.
    return [
        "nice", "-n", "19", "ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2",
        "-reconnect", "1", "-reconnect_on_network_error", "1",
        "-i", source_url, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", f"scale=-2:{HEIGHT},setsar=1,fps=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-maxrate", "1200k",
        "-bufsize", "2400k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", "-map_metadata", "-1", str(target),
    ]


class _SourceServer:
    """Loopback HTTP with Range, one random token per job; nothing else is served."""

    def __init__(self, reader) -> None:
        self._reader = reader
        self._jobs: dict[str, tuple[str, str, int]] = {}
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/src/{token}", self._serve)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def register(self, package_path: str, relpath: str, size: int) -> str:
        token = secrets.token_urlsafe(24)
        self._jobs[token] = (package_path, relpath, size)
        return f"http://127.0.0.1:{self.port}/src/{token}"

    def forget(self, url: str) -> None:
        self._jobs.pop(url.rsplit("/", 1)[-1], None)

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        job = self._jobs.get(request.match_info["token"])
        if job is None:
            raise web.HTTPNotFound()
        package_path, relpath, size = job
        start, end = 0, size - 1
        header = request.headers.get("Range", "")
        if header.startswith("bytes="):
            first, _, last = header[6:].partition("-")
            try:
                start = int(first) if first else max(0, size - int(last))
                end = min(int(last), size - 1) if first and last else size - 1
            except ValueError:
                raise web.HTTPRequestRangeNotSatisfiable() from None
        if not 0 <= start <= end < size:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        upstream = await self._reader.open_range(package_path, relpath, ByteRange(start, end))
        response = web.StreamResponse(status=206 if header else 200, headers={
            "Content-Type": "application/octet-stream", "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            **({"Content-Range": f"bytes {start}-{end}/{size}"} if header else {}),
        })
        try:
            if upstream.status not in {200, 206}:
                raise web.HTTPBadGateway()
            await response.prepare(request)
            try:
                async for chunk in upstream.body:
                    await response.write(chunk)
            except (ConnectionError, aiohttp.ClientConnectionResetError):
                pass  # ffmpeg hangs up once it has read enough, or seeks elsewhere
            return response
        finally:
            close = getattr(upstream.body, "aclose", None)
            if close is not None:
                await close()


class RenditionMaker:
    def __init__(
        self, repository, reader, *, work_dir: Path,
        writer: Callable[[], object], libraries: Callable[[], Awaitable[tuple[str, ...]]],
        is_playing: Callable[[], bool],
    ) -> None:
        self._repository = repository
        self._writer = writer
        self._libraries = libraries
        self._is_playing = is_playing
        self._work_dir = work_dir
        self._source = _SourceServer(reader)
        self._skip: set[tuple[str, object]] = set()  # failed or uploaded, until the scan links the file

    async def run(self, stop: asyncio.Event) -> None:
        self._work_dir.mkdir(parents=True, exist_ok=True)
        await self._source.start()
        try:
            while not stop.is_set():
                job = None
                if not self._is_playing():
                    # Covers first: one seek and one frame, and the wall needs them most.
                    covers = await self._repository.list_cover_candidates(limit=20)
                    job = next((("cover", item) for item in covers if ("cover", item["media_id"]) not in self._skip), None)
                    if job is None:
                        copies = await self._repository.list_rendition_candidates(limit=20)
                        job = next((("copy", item) for item in copies if ("copy", item["media_id"]) not in self._skip), None)
                if job is not None:
                    kind, item = job
                    self._skip.add((kind, item["media_id"]))
                    try:
                        await (self.make_cover(item) if kind == "cover" else self.make(item, stop))
                    except Exception as exc:
                        _LOG.warning("player.%s.failed media=%s error=%s",
                                     kind, str(item["media_id"])[:12], type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5 if job else 60)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._source.close()

    async def make(self, job: dict[str, object], stop: asyncio.Event) -> bool:
        media_id = str(job["media_id"])
        package_path = str(job["remote_path"])
        root = await self._root_for(package_path)
        if root is None:
            return False
        target = self._work_dir / f"{media_id}.{HEIGHT}.mp4"
        url = self._source.register(package_path, str(job["remote_relpath"]), int(job["size_bytes"]))
        try:
            duration = float(job["duration_seconds"])
            await self._encode(url, target, duration, stop)
            if stop.is_set():
                return False
            await self._validate(target, duration)
            await self._upload(target, f"{root}/{RENDITIONS_DIR}/{media_id}/{HEIGHT}.mp4")
            _LOG.info("player.rendition.uploaded media=%s bytes=%s", media_id[:12], target.stat().st_size)
            return True
        finally:
            self._source.forget(url)
            target.unlink(missing_ok=True)

    async def make_cover(self, job: dict[str, object]) -> bool:
        media_id = str(job["media_id"])
        package_path = str(job["remote_path"])
        root = await self._root_for(package_path)
        if root is None:
            return False
        target = self._work_dir / f"{media_id}.cover.jpg"
        url = self._source.register(package_path, str(job["remote_relpath"]), int(job["size_bytes"]))
        try:
            # A frame a tenth in, past intros and black openings; no later than ten minutes.
            at = min(float(job["duration_seconds"]) * 0.1, 600.0)
            await _run("nice", "-n", "19", "ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{at:.2f}",
                       "-i", url, "-frames:v", "1", "-vf", "scale=-2:720", "-q:v", "4", str(target), timeout=180)
            if not target.exists() or target.stat().st_size == 0:
                raise ValueError("no frame")
            await self._upload(target, f"{root}/{RENDITIONS_DIR}/{media_id}/{COVER_FILE}", "image/jpeg")
            return True
        finally:
            self._source.forget(url)
            target.unlink(missing_ok=True)

    async def _root_for(self, package_path: str) -> str | None:
        return next((r for r in await self._libraries()
                     if package_path == r or package_path.startswith(r + "/")), None)

    async def _encode(self, url: str, target: Path, duration: float, stop: asyncio.Event) -> None:
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_args(url, target), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        deadline = asyncio.get_running_loop().time() + max(1800.0, duration * 10)
        paused = False
        try:
            while proc.returncode is None:
                if stop.is_set() or asyncio.get_running_loop().time() > deadline:
                    raise asyncio.CancelledError() if stop.is_set() else TimeoutError("encode too slow")
                playing = self._is_playing()
                if playing != paused:
                    proc.send_signal(signal.SIGSTOP if playing else signal.SIGCONT)
                    paused = playing
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
        finally:
            if proc.returncode is None:
                if paused:
                    proc.send_signal(signal.SIGCONT)
                proc.kill()
                await proc.wait()
        if proc.returncode:
            raise RuntimeError("ffmpeg failed")

    @staticmethod
    async def _validate(target: Path, duration: float) -> None:
        info = json.loads(await _run(
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(target), timeout=60))
        video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        actual = float(info.get("format", {}).get("duration") or 0)
        if (len(video) != 1 or video[0].get("height") != HEIGHT or not math.isfinite(actual)
                or abs(actual - duration) > max(2.0, duration * 0.01)):
            raise ValueError("copy does not match the original")

    async def _upload(self, source: Path, path: str, content_type: str = "video/mp4") -> None:
        writer = self._writer()
        await writer.ensure_directory(path.rsplit("/", 1)[0])
        size = source.stat().st_size

        async def chunks():
            with source.open("rb") as handle:
                while block := await asyncio.to_thread(handle.read, _CHUNK):
                    yield block

        receipt = await writer.put_stream(path, chunks(), size_bytes=size, content_type=content_type)
        if getattr(receipt, "size_bytes", size) != size:
            raise ValueError("upload size mismatch")
