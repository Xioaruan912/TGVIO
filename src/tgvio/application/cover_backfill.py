"""Bounded cover backfill for archive packages that were committed without covers.

Packages committed before covers existed cannot gain one through the manifest:
the archive verifies a committed manifest against the stored ``manifest_sha256``,
so rewriting ``manifest.json`` would mark the package corrupt. The backfill
therefore produces a **sidecar** - cover objects plus a ``covers.json`` index -
which a reader may consume without touching ``manifest.json`` or
``_COMPLETE.json``.

The module is deliberately pure: bounded head-reading and frame extraction are
injected as ports, so it can be unit tested without network, ffmpeg or a clock.
It never raises for a per-item failure, never exceeds its byte/deadline budgets,
and never samples anything that is not a video.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Awaitable, Callable, Iterable, Mapping
import hashlib
import json
import time

COVERS_INDEX_NAME = "covers.json"
COVERS_PREFIX = "cover/"
COVERS_SCHEMA = "tgvio.archive.covers/v1"
COVER_MIME_TYPE = "image/jpeg"
BACKFILL_ALGORITHM = "backfill-head-frame-v1"
DEFAULT_MAX_COVER_BYTES = 1_000_000

# A port: read a bounded head of the video and return one JPEG frame, or None.
FrameFromHead = Callable[[str], Awaitable[bytes | None]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class BackfillTarget:
    """One archived video that may receive a cover."""

    media_path: str
    source_path: str
    item_index: int


@dataclass(frozen=True)
class BackfillCover:
    media_path: str
    cover_relpath: str
    payload: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.payload)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    def index_entry(self) -> dict[str, object]:
        return {
            "path": self.cover_relpath,
            "size_bytes": self.size_bytes,
            "mime_type": COVER_MIME_TYPE,
            "sha256": self.sha256,
        }


@dataclass
class BackfillResult:
    covers: tuple[BackfillCover, ...] = ()
    skipped: tuple[str, ...] = ()
    reason: str = ""


def cover_relpath_for(media_path: str, taken: set[str] | None = None) -> str:
    """One naming rule shared with archive-time covers, deduplicated on collision."""
    stem = PurePosixPath(media_path).stem or "media"
    taken = taken if taken is not None else set()
    candidate = f"{COVERS_PREFIX}{stem}-cover.jpg"
    suffix = 2
    while candidate in taken:
        candidate = f"{COVERS_PREFIX}{stem}-cover-{suffix}.jpg"
        suffix += 1
    taken.add(candidate)
    return candidate


def build_covers_index(covers: Iterable[BackfillCover]) -> dict[str, object]:
    entries: dict[str, object] = {}
    for cover in sorted(covers, key=lambda item: item.media_path):
        entries[cover.media_path] = cover.index_entry()
    return {"schema": COVERS_SCHEMA, "algorithm": BACKFILL_ALGORITHM, "covers": entries}


def parse_covers_index(payload: bytes | str | Mapping[str, object]) -> dict[str, dict[str, object]]:
    """Read a sidecar index defensively; an unusable index simply has no covers."""
    if isinstance(payload, (bytes, str)):
        try:
            document = json.loads(payload)
        except (ValueError, TypeError):
            return {}
    else:
        document = payload
    if not isinstance(document, Mapping) or document.get("schema") != COVERS_SCHEMA:
        return {}
    covers = document.get("covers")
    if not isinstance(covers, Mapping):
        return {}
    parsed: dict[str, dict[str, object]] = {}
    for media_path, entry in covers.items():
        if not isinstance(media_path, str) or not isinstance(entry, Mapping):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith(COVERS_PREFIX):
            continue
        parsed[media_path] = {
            "path": path,
            "size_bytes": int(entry.get("size_bytes") or 0),
            "mime_type": str(entry.get("mime_type") or COVER_MIME_TYPE),
        }
    return parsed


def targets_from_manifest(manifest: Mapping[str, object]) -> tuple[BackfillTarget, ...]:
    """Every archived video that could still receive a cover, in manifest order.

    Non-video entries and entries that already carry an archive-time cover are
    skipped, so a package produced after A2 is never backfilled twice.
    """
    media = manifest.get("media") if isinstance(manifest, Mapping) else None
    if not isinstance(media, (list, tuple)):
        return ()
    targets: list[BackfillTarget] = []
    for entry in media:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("kind") or "") != "video":
            continue
        media_path = entry.get("path")
        if not isinstance(media_path, str) or not media_path:
            continue
        if entry.get("cover"):
            continue
        try:
            index = int(entry.get("index") or 0) - 1
        except (TypeError, ValueError):
            index = -1
        targets.append(
            BackfillTarget(media_path=media_path, source_path=media_path, item_index=index)
        )
    return tuple(targets)


class CoverBackfill:
    """Sample at most one bounded frame per archived video, resumably."""

    def __init__(
        self,
        frame_from_head: FrameFromHead,
        *,
        max_cover_bytes: int = DEFAULT_MAX_COVER_BYTES,
        total_deadline_seconds: float = 900.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self._frame_from_head = frame_from_head
        self._max_cover_bytes = max(1, int(max_cover_bytes))
        self._total_deadline_seconds = max(1.0, float(total_deadline_seconds))
        self._clock = clock

    async def run(
        self,
        targets: Iterable[BackfillTarget],
        *,
        already_covered: Iterable[str] = (),
    ) -> BackfillResult:
        done = {str(path) for path in already_covered}
        taken: set[str] = set()
        covers: list[BackfillCover] = []
        skipped: list[str] = []
        deadline = self._clock() + self._total_deadline_seconds
        reason = ""
        for target in targets:
            if target.media_path in done:
                continue
            if self._clock() >= deadline:
                reason = "deadline"
                skipped.append(target.media_path)
                continue
            payload = await self._frame(target.source_path)
            if payload is None or not 0 < len(payload) <= self._max_cover_bytes:
                skipped.append(target.media_path)
                continue
            covers.append(
                BackfillCover(
                    media_path=target.media_path,
                    cover_relpath=cover_relpath_for(target.media_path, taken),
                    payload=payload,
                )
            )
        if not covers:
            reason = reason or "nothing_produced"
        return BackfillResult(covers=tuple(covers), skipped=tuple(skipped), reason=reason)

    async def _frame(self, source_path: str) -> bytes | None:
        """A cover is optional: a failing port just yields none for that item."""
        try:
            payload = await self._frame_from_head(source_path)
        except Exception:
            return None
        return payload if isinstance(payload, bytes) and payload else None
