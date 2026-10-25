"""Cover fingerprints, identical to the ones the maintenance container wrote.

Duplicate review compares these; a cover the player makes must hash the same way
as the covers already in covers.json, or old and new videos never match.
"""
from __future__ import annotations

GRAY_EDGE = 32
HASH_COLUMNS = 9
HASH_ROWS = 8


def _box_mean(gray: bytes, row: int, column: int) -> int:
    rows = range(row * GRAY_EDGE // HASH_ROWS, (row + 1) * GRAY_EDGE // HASH_ROWS)
    columns = range(column * GRAY_EDGE // HASH_COLUMNS, (column + 1) * GRAY_EDGE // HASH_COLUMNS)
    total = sum(gray[r * GRAY_EDGE + c] for r in rows for c in columns)
    return total // (len(rows) * len(columns))


def dhash_gray32(gray: bytes) -> str:
    """A 64-bit difference hash of one 32x32 gray frame, as 16 lowercase hex digits."""
    if len(gray) != GRAY_EDGE * GRAY_EDGE:
        raise ValueError("cover hash needs a 32x32 gray frame")
    grid = [[_box_mean(gray, row, column) for column in range(HASH_COLUMNS)] for row in range(HASH_ROWS)]
    value = 0
    for row in range(HASH_ROWS):
        for column in range(HASH_COLUMNS - 1):
            if grid[row][column] > grid[row][column + 1]:
                value |= 1 << (63 - (row * 8 + column))
    return f"{value:016x}"


def flat_extreme_frame(gray: bytes) -> bool:
    """Featureless black or white (a fade, a title card), but not dark or bright detail."""
    count = len(gray)
    if not count:
        return True
    dominated = sum(v < 24 for v in gray) >= count * .95 or sum(v > 231 for v in gray) >= count * .95
    if not dominated:
        return False
    total = sum(gray)
    return sum(v * v for v in gray) * count - total * total < 16 * count * count
