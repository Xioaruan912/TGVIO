"""Pure rendition sidecar identity and archive-relative naming."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

SCHEMA = "tgvio.archive.renditions/v1"
INDEX = "renditions.json"
ALGORITHM = "h264-height-crf24-v1"
HEIGHTS = (480, 720)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "%" in value:
        raise ValueError("unsafe archive path")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("unsafe archive path")
    if value.startswith("/") or any(x in ("", ".", "..") for x in value.split("/")):
        raise ValueError("unsafe archive path")
    return PurePosixPath(value).as_posix()


@dataclass(frozen=True)
class SampledFrame:
    """One decoded still and the fingerprint computed from the same pixels.

    The two travel together because they are derived from one frame: a caller cannot
    hold a hash that belongs to a different picture than the bytes it stores.
    """

    payload: bytes
    phash: str


@dataclass(frozen=True)
class RenditionTask:
    package_id: str
    root: str
    manifest_hash: str
    media: dict[str, Any]

    @property
    def key(self) -> str:
        return hashlib.sha256(canonical([self.package_id, self.manifest_hash,
                                        self.media["sha256"], self.media["path"]])).hexdigest()

    @property
    def required(self) -> tuple[int, ...]:
        height = self.media.get("height")
        return tuple(h for h in HEIGHTS if not isinstance(height, int) or height > h)
