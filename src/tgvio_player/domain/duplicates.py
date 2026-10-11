"""Which videos look like the same video: near-identical covers and lengths."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Cover fingerprints at most this far apart (of 64 bits) are the same frame.
MAX_DISTANCE = 6
# Lengths within this share of each other, or two seconds, are the same length.
DURATION_TOLERANCE = 0.03
MIN_DURATION_SLACK = 2.0


@dataclass(frozen=True, slots=True)
class Candidate:
    media_id: str
    phash: str
    duration: float


def _distance(left: str, right: str) -> int:
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def duplicate_groups(
    candidates: Iterable[Candidate], dismissed: set[tuple[str, str]]
) -> list[list[str]]:
    """Groups of two or more media ids, each group and its members sorted.

    Videos are compared only with others of about the same length (a sorted
    sweep), and a pair the viewer dismissed never links two videos.
    """
    items = sorted(candidates, key=lambda c: (c.duration, c.media_id))
    parent = {item.media_id: item.media_id for item in items}

    def root(media_id: str) -> str:
        while parent[media_id] != media_id:
            parent[media_id] = parent[parent[media_id]]
            media_id = parent[media_id]
        return media_id

    for index, left in enumerate(items):
        slack = max(MIN_DURATION_SLACK, left.duration * DURATION_TOLERANCE)
        for right in items[index + 1:]:
            if right.duration - left.duration > slack:
                break
            if (_distance(left.phash, right.phash) <= MAX_DISTANCE
                    and pair(left.media_id, right.media_id) not in dismissed):
                parent[root(left.media_id)] = root(right.media_id)
    groups: dict[str, list[str]] = {}
    for item in items:
        groups.setdefault(root(item.media_id), []).append(item.media_id)
    return sorted(sorted(group) for group in groups.values() if len(group) > 1)
