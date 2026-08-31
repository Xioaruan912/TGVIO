import asyncio
import json
import logging
import mimetypes
import os
import re
import struct
import weakref
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}
_VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "wmv", "3gp", "ts"}
_THUMB_MAX = 320
_THUMB_MAX_BYTES = 40 * 1024
_MEDIA_PROCESS_CONCURRENCY = 2
_MEDIA_PROCESS_LIMITS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
_FFPROBE_TIMEOUT = 30.0
_FFMPEG_TIMEOUT = 180.0


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _media_process_limit() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limit = _MEDIA_PROCESS_LIMITS.get(loop)
    if limit is None:
        limit = asyncio.Semaphore(_MEDIA_PROCESS_CONCURRENCY)
        _MEDIA_PROCESS_LIMITS[loop] = limit
    return limit


async def _run_media_process(cmd: list[str], *, timeout: float) -> _ProcessResult:
    """Run ffmpeg/ffprobe with bounded concurrency and hard cancellation."""
    async with _media_process_limit():
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise
    return _ProcessResult(
        returncode=int(process.returncode or 0),
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


@dataclass(frozen=True)
class MediaMetadata:
    path: str
    container: str
    video_codec: str | None
    audio_codec: str | None
    duration_seconds: float
    width: int
    height: int
    rotation: int
    bitrate: int
    stream_count: int
    faststart: bool | None

    @property
    def telegram_streaming_ready(self) -> bool:
        container_ok = any(token in self.container for token in ("mp4", "mov", "m4a", "3gp"))
        video_ok = self.video_codec in (None, "h264")
        audio_ok = self.audio_codec in (None, "aac")
        return bool(container_ok and self.faststart is True and video_ok and audio_ok)

    @property
    def needs_faststart(self) -> bool:
        return self.faststart is False and any(
            token in self.container for token in ("mp4", "mov", "m4a", "3gp")
        )


async def probe_media_metadata(path: str) -> MediaMetadata:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration,bit_rate",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,duration,bit_rate:stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        path,
    ]
    result = await _run_media_process(cmd, timeout=_FFPROBE_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {result.stderr.strip()[-500:] or 'unknown'}")
    data = json.loads(result.stdout or "{}")
    streams = list(data.get("streams") or [])
    fmt = data.get("format") or {}
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    duration_raw = fmt.get("duration") or video.get("duration") or audio.get("duration") or 0
    try:
        duration = max(0.0, float(duration_raw))
    except (TypeError, ValueError):
        duration = 0.0
    bitrate_raw = fmt.get("bit_rate") or video.get("bit_rate") or 0
    try:
        bitrate = max(0, int(float(bitrate_raw)))
    except (TypeError, ValueError):
        bitrate = 0
    rotation = 0
    candidate = (video.get("tags") or {}).get("rotate")
    if candidate is None:
        for item in video.get("side_data_list") or []:
            if item.get("rotation") is not None:
                candidate = item.get("rotation")
                break
    try:
        rotation = int(float(candidate or 0)) % 360
    except (TypeError, ValueError):
        rotation = 0
    container = str(fmt.get("format_name") or "").lower()
    faststart = (
        await asyncio.to_thread(_mp4_faststart_sync, path)
        if _looks_like_mp4(path, container)
        else None
    )
    return MediaMetadata(
        path=os.path.realpath(path),
        container=container,
        video_codec=str(video.get("codec_name")) if video.get("codec_name") else None,
        audio_codec=str(audio.get("codec_name")) if audio.get("codec_name") else None,
        duration_seconds=duration,
        width=max(0, int(video.get("width") or 0)),
        height=max(0, int(video.get("height") or 0)),
        rotation=rotation,
        bitrate=bitrate,
        stream_count=len(streams),
        faststart=faststart,
    )


def _looks_like_mp4(path: str, container: str) -> bool:
    return Path(path).suffix.lower() in {".mp4", ".m4v", ".mov", ".3gp"} or any(
        token in container for token in ("mp4", "mov", "m4a", "3gp")
    )


def _mp4_faststart_sync(path: str) -> bool | None:
    """Return whether the top-level moov atom precedes mdat."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            offset = 0
            moov = mdat = None
            while offset + 8 <= size:
                handle.seek(offset)
                header = handle.read(8)
                if len(header) < 8:
                    break
                atom_size = struct.unpack(">I", header[:4])[0]
                atom_type = header[4:8]
                header_size = 8
                if atom_size == 1:
                    ext = handle.read(8)
                    if len(ext) != 8:
                        return None
                    atom_size = struct.unpack(">Q", ext)[0]
                    header_size = 16
                elif atom_size == 0:
                    atom_size = size - offset
                if atom_size < header_size or offset + atom_size > size:
                    return None
                if atom_type == b"moov" and moov is None:
                    moov = offset
                elif atom_type == b"mdat" and mdat is None:
                    mdat = offset
                if moov is not None and mdat is not None:
                    return moov < mdat
                offset += atom_size
    except (OSError, struct.error):
        return None
    return None


async def remux_faststart(path: str, output_path: str | None = None) -> str:
    source = Path(path).resolve()
    target = Path(output_path).resolve() if output_path else source.with_name(
        f"{source.stem}.faststart{source.suffix or '.mp4'}"
    )
    if target.parent != source.parent:
        raise ValueError("faststart output must remain in the same job directory")
    if target == source:
        raise ValueError("faststart output must differ from source")
    result = await _run_media_process(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0",
            "-c", "copy", "-movflags", "+faststart", str(target),
        ],
        timeout=_FFMPEG_TIMEOUT,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        try:
            target.unlink()
        except OSError:
            pass
        raise RuntimeError(f"faststart remux 失败: {result.stderr.strip()[-500:] or 'unknown'}")
    before = await probe_media_metadata(str(source))
    after = await probe_media_metadata(str(target))
    tolerance = max(1.0, before.duration_seconds * 0.02)
    valid = (
        before.stream_count == after.stream_count
        and abs(before.duration_seconds - after.duration_seconds) <= tolerance
        and after.faststart is True
    )
    if not valid:
        try:
            target.unlink()
        except OSError:
            pass
        raise RuntimeError("faststart remux 校验失败")
    return str(target)


async def probe_video(path: str) -> tuple[int, int, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        path,
    ]
    result = await _run_media_process(cmd, timeout=_FFPROBE_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {result.stderr.strip()[-500:] or 'unknown'}")

    data = json.loads(result.stdout or "{}")
    duration = width = height = None
    streams = data.get("streams") or []
    if streams:
        stream = streams[0]
        width = stream.get("width")
        height = stream.get("height")
        duration = stream.get("duration")
    if not duration:
        duration = (data.get("format") or {}).get("duration")

    try:
        duration = int(float(duration or 0))
    except (TypeError, ValueError):
        duration = 0
    return duration, int(width or 1), int(height or 1)


async def make_cover(path: str, workdir: str, max_w: int = 1280) -> str:
    out = os.path.join(workdir, "cover.jpg")
    for seek in ("1", "0"):
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            seek,
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            f"scale={max_w}:{max_w}:force_original_aspect_ratio=decrease",
            "-q:v",
            "3",
            out,
        ]
        result = await _run_media_process(cmd, timeout=_FFMPEG_TIMEOUT)
        if result.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    detail = result.stderr.strip()[-500:] if result.stderr.strip() else "no frames"
    raise RuntimeError(f"生成封面图失败: {detail}")


async def make_thumb(path: str, workdir: str, position: str = "auto") -> str | None:
    out = os.path.join(workdir, "thumb.jpg")
    for seek in (await _thumbnail_seeks(path, position))[:3]:
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{seek:.3f}",
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            f"scale={_THUMB_MAX}:{_THUMB_MAX}:force_original_aspect_ratio=decrease",
            "-q:v",
            "5",
            out,
        ]
        result = await _run_media_process(cmd, timeout=_FFMPEG_TIMEOUT)
        if result.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            if await _frame_is_black(out):
                try:
                    os.remove(out)
                except OSError:
                    pass
                continue
            size = os.path.getsize(out)
            if size > _THUMB_MAX_BYTES:
                logger.warning("Thumb too large (%d bytes), dropping", size)
                try:
                    os.remove(out)
                except OSError:
                    pass
                return None
            return out
    detail = result.stderr.strip()[-500:] if result.stderr.strip() else "no frames"
    raise RuntimeError(f"生成缩略图失败: {detail}")


async def _thumbnail_seeks(path: str, position: str = "auto") -> list[float]:
    if position != "auto":
        try:
            return [max(0.0, float(position)), 1.0, 0.0]
        except (TypeError, ValueError):
            pass
    try:
        duration, _, _ = await probe_video(path)
    except Exception:
        duration = 0
    if duration > 0:
        return [duration * ratio for ratio in (0.10, 0.20, 0.30)]
    return [1.0, 0.0]


async def _frame_is_black(path: str) -> bool:
    result = await _run_media_process(
        [
            "ffmpeg", "-v", "info", "-i", path,
            "-vf", "blackframe=amount=95:threshold=32", "-f", "null", "-",
        ],
        timeout=_FFMPEG_TIMEOUT,
    )
    match = re.search(r"pblack:(\d+)", result.stderr or "")
    return bool(match and int(match.group(1)) >= 95)


def is_photo_path(path: str) -> bool:
    return _guess_mime(path).startswith("image/")


def is_video_path(path: str) -> bool:
    return _guess_mime(path).startswith("video/")


def guess_mime(path: str) -> str:
    return _guess_mime(path)


def _guess_mime(path: str) -> str:
    mime = mimetypes.guess_type(path)[0]
    if mime:
        return mime
    ext = _ext(path)
    if ext in _IMAGE_EXTS:
        return f"image/{ext}"
    if ext in _VIDEO_EXTS:
        return f"video/{ext}"
    return "application/octet-stream"


def _ext(path: str) -> str:
    return Path(path).suffix.lower().lstrip(".")
