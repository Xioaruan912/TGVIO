"""Decode one real still from bounded head/tail ranges, including tail-moov MP4."""
from __future__ import annotations

import asyncio
from pathlib import Path


async def command(*args: str) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), 12)
        return proc.returncode or 0, output
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise


def flat_extreme_frame(gray: bytes) -> bool:
    """Reject featureless black/white, without discarding bright or dark detail."""
    count = len(gray)
    if not count:
        return True
    dominated = sum(v < 24 for v in gray) >= count * .95 or sum(v > 231 for v in gray) >= count * .95
    if not dominated:
        return False
    total = sum(gray)
    # Standard deviation >= 4 and a meaningful luma range preserve small
    # content on white backgrounds and highlights in dark scenes.
    variance_numerator = sum(v*v for v in gray) * count - total*total
    return max(gray) - min(gray) < 32 or variance_numerator < 16 * count * count


async def extract(source: Path, work: Path) -> bytes | None:
    output = work / "frame.jpg"
    for position in ("1", "0.1", "3", "0"):
        output.unlink(missing_ok=True)
        try:
            code, _ = await command(
                "ffmpeg", "-nostdin", "-y", "-v", "fatal", "-threads", "1",
                "-filter_threads", "1", "-xerror", "-err_detect", "explode",
                "-ss", position, "-i", str(source), "-map", "0:v:0",
                "-frames:v", "1", "-threads", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
                "-q:v", "5", "-fs", "1000000", str(output))
        except TimeoutError:
            continue
        if code or not output.is_file() or not 0 < output.stat().st_size <= 1_000_000:
            continue
        try:
            code, gray = await command(
                "ffmpeg", "-nostdin", "-v", "fatal", "-threads", "1", "-filter_threads", "1",
                "-i", str(output), "-frames:v", "1", "-threads", "1", "-vf", "scale=32:32", "-pix_fmt", "gray",
                "-f", "rawvideo", "-")
        except TimeoutError:
            continue
        if code or len(gray) != 1024:
            continue
        if flat_extreme_frame(gray):
            continue
        payload = output.read_bytes()
        if payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9"):
            return payload
    return None


async def sample(reader, path: str, size: int, work: Path) -> bytes | None:
    if type(size) is not int or not 0 < size <= 4 * 1024**3:
        raise ValueError("cover source outside size budget")
    source = work / "sample.bin"
    # Holes are local zeros; only filled ranges can decode under -xerror.
    with source.open("wb") as file:
        file.truncate(size)
    head = 0
    tail_start = size
    for wanted_head, wanted_tail in ((1024**2, 512*1024), (2*1024**2, 2*1024**2),
                                     (8*1024**2, 4*1024**2)):
        end = min(size, wanted_head, tail_start)
        if end > head:
            data = await reader.read_range(path, head, end - 1, size)
            with source.open("r+b") as file:
                file.seek(head); file.write(data)
            head = end
        start = max(head, size - wanted_tail)
        if start < tail_start:
            data = await reader.read_range(path, start, tail_start - 1, size)
            with source.open("r+b") as file:
                file.seek(start); file.write(data)
            tail_start = start
        frame = await extract(source, work)
        if frame:
            return frame
    return None
