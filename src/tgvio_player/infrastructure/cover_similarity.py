"""Fingerprint distance as a pure function, so the budget gate does not force it into a
repository that is already at its ceiling.

The 64-bit dHash lives in the covers index as 16 lowercase hex digits. A value this module
cannot read is skipped, never compared: two unreadable fingerprints are not "identical",
they are unknown, and unknown is not a match.
"""
from __future__ import annotations

import re

FINGERPRINT = re.compile(r"^[0-9a-f]{16}$")

# Near-duplicates of the same frame, and the band a viewer would call "similar".
DUPLICATE_DISTANCE = 6
SIMILAR_DISTANCE = 16


def is_fingerprint(value: object) -> bool:
    return isinstance(value, str) and FINGERPRINT.fullmatch(value) is not None


def hamming(left: str, right: str) -> int:
    if not is_fingerprint(left) or not is_fingerprint(right):
        raise ValueError("a fingerprint is 16 lowercase hex digits")
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def nearest_fingerprints(
    rows: object, phash: str, *, threshold: int, limit: int
) -> tuple[tuple[str, int], ...]:
    """The nearest readable rows to this fingerprint, as (media_id, distance).

    `rows` yields (media_id, phash) pairs. A media that carries several covers keeps its
    nearest one; ties break by media_id, so the same request always answers the same way.
    """
    if not is_fingerprint(phash):
        return ()
    nearest: dict[str, int] = {}
    for media_id, candidate in rows:  # type: ignore[misc]
        if not is_fingerprint(candidate):
            continue
        distance = hamming(phash, str(candidate))
        if distance > threshold:
            continue
        key = str(media_id)
        if distance < nearest.get(key, 65):
            nearest[key] = distance
    ordered = sorted(nearest.items(), key=lambda item: (item[1], item[0]))
    return tuple(ordered[: max(1, int(limit))])
