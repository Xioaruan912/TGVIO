import asyncio
import json
import logging
import mimetypes
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}
_VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "wmv", "3gp", "ts"}
_THUMB_MAX = 320
_THUMB_MAX_BYTES = 40 * 1024


async def probe_video(path: str) -> tuple[int, int, int]:
    return await asyncio.to_thread(_probe_video_sync, path)


def _probe_video_sync(path: str) -> tuple[int, int, int]:
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
    result = subprocess.run(cmd, capture_output=True, text=True)
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
    return await asyncio.to_thread(_make_cover_sync, path, workdir, max_w)


def _make_cover_sync(path: str, workdir: str, max_w: int = 1280) -> str:
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
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    detail = result.stderr.strip()[-500:] if result.stderr.strip() else "no frames"
    raise RuntimeError(f"生成封面图失败: {detail}")


async def make_thumb(path: str, workdir: str) -> str | None:
    return await asyncio.to_thread(_make_thumb_sync, path, workdir)


def _make_thumb_sync(path: str, workdir: str) -> str | None:
    out = os.path.join(workdir, "thumb.jpg")
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
            f"scale={_THUMB_MAX}:{_THUMB_MAX}:force_original_aspect_ratio=decrease",
            "-q:v",
            "5",
            out,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
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
