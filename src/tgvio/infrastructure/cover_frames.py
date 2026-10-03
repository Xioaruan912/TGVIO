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


GRAY_EDGE = 32
HASH_COLUMNS = 9
HASH_ROWS = 8


def _box_mean(gray: bytes, row: int, column: int) -> int:
    """The mean of the source block this hash cell covers, as an integer.

    Box-averaging (rather than an extra ffmpeg resize) keeps the fingerprint a pure
    function of bytes we already have, so the same frame always yields the same bits.
    """
    rows = range(row * GRAY_EDGE // HASH_ROWS, (row + 1) * GRAY_EDGE // HASH_ROWS)
    columns = range(
        column * GRAY_EDGE // HASH_COLUMNS, (column + 1) * GRAY_EDGE // HASH_COLUMNS
    )
    total = sum(gray[r * GRAY_EDGE + c] for r in rows for c in columns)
    return total // (len(rows) * len(columns))


def dhash_gray32(gray: bytes) -> str:
    """A 64-bit difference hash of one 32x32 gray frame, as 16 lowercase hex digits.

    Bit 0 is the most significant bit: row 0's leftmost comparison. A frame this build
    cannot read is a programming error, never a fingerprint of zero.
    """
    if len(gray) != GRAY_EDGE * GRAY_EDGE:
        raise ValueError("cover hash needs a 32x32 gray frame")
    grid = [
        [_box_mean(gray, row, column) for column in range(HASH_COLUMNS)]
        for row in range(HASH_ROWS)
    ]
    value = 0
    for row in range(HASH_ROWS):
        for column in range(HASH_COLUMNS - 1):
            if grid[row][column] > grid[row][column + 1]:
                value |= 1 << (63 - (row * 8 + column))
    return f"{value:016x}"


def flat_extreme_frame(gray: bytes) -> bool:
    """Reject featureless black/white, without discarding bright or dark detail."""
    count = len(gray)
    if not count:
        return True
    dominated = sum(v < 24 for v in gray) >= count * .95 or sum(v > 231 for v in gray) >= count * .95
    if not dominated:
        return False
    total = sum(gray)
    # A standard deviation >= 4 preserves real low contrast detail even
    # when all pixels fall inside the dark or bright band.
    variance_numerator = sum(v*v for v in gray) * count - total*total
    return variance_numerator < 16 * count * count


async def extract(source: Path, work: Path, *,
                  positions: tuple[str, ...] = ("1", "0.1", "3", "0")) -> bytes | None:
    output = work / "frame.jpg"
    for position in positions:
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
    # A dark intro can hide content already in the filled prefix. Two fixed
    # fallback positions reuse the existing ranges and the outer 180s deadline.
    return await extract(source, work, positions=("5", "10"))
