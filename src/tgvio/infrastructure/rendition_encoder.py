"""Bounded progressive H.264 encodes with cancellation and ffprobe validation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path


async def run_process(*args: str, timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode:
            raise RuntimeError("media command failed")
        return out.decode("utf-8")
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


async def probe(path: Path) -> dict:
    return json.loads(await run_process(
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
        str(path), timeout=45))


async def encode(source: Path, target: Path, height: int) -> dict:
    if height not in (480, 720):
        raise ValueError("unsupported rendition height")
    info = await probe(source)
    streams = [s for s in info["streams"] if s.get("codec_type") == "video"
               and not s.get("disposition", {}).get("attached_pic")]
    if not streams:
        raise ValueError("source has no video")
    duration = float(info["format"].get("duration") or streams[0].get("duration") or 0)
    if not math.isfinite(duration) or not 0 < duration <= 24 * 3600:
        raise ValueError("unknown or excessive duration")
    if int(streams[0]["height"]) <= height:
        raise ValueError("refusing rendition upscale")
    source_digest = await asyncio.to_thread(file_hash, source)
    limit = "1200k" if height == 480 else "2500k"
    buffer = "2400k" if height == 480 else "5000k"
    temporary = target.with_suffix(".part.mp4")
    try:
        await run_process(
            "ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2",
            "-filter_threads", "1", "-i", str(source), "-map", "0:v:0",
            "-map", "0:a:0?", "-sn", "-dn", "-vf", f"scale=-2:{height},setsar=1,fps=30",
            "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "24",
            "-maxrate", limit, "-bufsize", buffer, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            "-map_metadata", "-1", "-metadata", f"comment=tgvio.source_sha256:{source_digest}",
            "-fs", str(4 * 1024**3), str(temporary),
            timeout=min(24 * 3600, max(600, duration * 8 + 300)))
        result = await probe(temporary)
        video = next(s for s in result["streams"] if s.get("codec_type") == "video")
        actual_duration = float(result["format"]["duration"])
        if (video.get("codec_name") != "h264" or video["height"] != height
                or abs(actual_duration - duration) > max(1, duration * .01)):
            raise ValueError("rendition validation failed")
        size = temporary.stat().st_size
        if not 0 < size <= 4 * 1024**3:
            raise ValueError("rendition file size invalid")
        temporary.replace(target)
        return {"sha256": await asyncio.to_thread(file_hash, target),
                "size_bytes": size, "width": video["width"], "height": video["height"],
                "duration_seconds": actual_duration,
                "bitrate_bps": round(size * 8 / actual_duration)}
    finally:
        temporary.unlink(missing_ok=True)


async def verify_recovered(path: Path, parent: str, height: int, duration: float,
                           digest: str) -> dict:
    """Validate only our source-bound H.264 output, including a complete decode."""
    info = await probe(path)
    video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"
             and not s.get("disposition", {}).get("attached_pic")]
    actual = float(info.get("format", {}).get("duration", 0))
    fmt = info.get("format", {})
    if (len(video) != 1 or video[0].get("codec_name") != "h264"
            or video[0].get("height") != height
            or type(video[0].get("width")) is not int or video[0]["width"] <= 0
            or not math.isfinite(actual) or actual <= 0
            or abs(actual - duration) > max(1, duration * .01)
            or "mp4" not in fmt.get("format_name", "").split(",")
            or fmt.get("tags", {}).get("comment") != f"tgvio.source_sha256:{parent}"):
        raise ValueError("recovery media binding invalid")
    await run_process(
        "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
        "-threads", "2", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
        "-sn", "-dn", "-f", "null", "-", timeout=60)
    size = path.stat().st_size
    return {"sha256": digest, "size_bytes": size, "height": height,
            "width": video[0]["width"], "duration_seconds": actual,
            "bitrate_bps": round(size * 8 / actual)}
