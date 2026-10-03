from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class CatalogValidationError(ValueError):
    """A remote Archive package does not satisfy the Player catalog contract."""


# A cover is a small still the archive may ship next to a video. The numbers and
# algorithm names below mirror the archive writer (``tgvio.archive*``); the
# manifest is the contract, so an unknown algorithm is treated as "no cover"
# rather than as a reason to hide the video.
MAX_COVER_BYTES = 1_000_000
COVER_ALGORITHMS = frozenset({"reuse-publish-thumbnail-v1"})
COVER_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


@dataclass(frozen=True, slots=True)
class ArchivePackageCandidate:
    remote_path: str
    manifest: Any
    complete: Any
    manifest_etag: str | None = None
    complete_etag: str | None = None
    renditions: Any = None
    covers: Any = None
    cover_read_failed: bool = False


@dataclass(frozen=True, slots=True)
class ArchiveDiscovery:
    packages: tuple[ArchivePackageCandidate, ...]
    complete_scan: bool = True


@dataclass(frozen=True, slots=True)
class CatalogMedia:
    media_id: str
    kind: str
    size_bytes: int
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    container: str | None = None
    codec: str | None = None
    variant_of: str | None = None
    label: str | None = None
    bitrate_bps: int | None = None

    @property
    def is_rendition(self) -> bool:
        return bool(self.variant_of)


@dataclass(frozen=True, slots=True)
class CatalogCover:
    """One bounded, versioned still that belongs to a catalog video."""

    media_id: str
    package_id: str
    remote_relpath: str
    size_bytes: int
    mime_type: str
    algorithm: str
    # 16 lowercase hex digits, or None for "no similarity information".
    phash: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogLocation:
    media_id: str
    package_id: str
    remote_relpath: str
    remote_etag: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogPackage:
    package_id: str
    remote_path: str
    manifest_sha256: str
    manifest_etag: str | None
    complete_etag: str | None
    media: tuple[CatalogMedia, ...]
    locations: tuple[CatalogLocation, ...]
    covers: tuple[CatalogCover, ...] = ()
    keep_existing_covers: bool = False


@dataclass(frozen=True, slots=True)
class CatalogSyncResult:
    discovered: int
    committed: int
    rejected: int
    inactive_packages: int
    active_videos: int
    errors: tuple[str, ...] = ()


def safe_remote_path(value: object, *, relative: bool) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise CatalogValidationError("archive path is empty")
    if "\\" in raw or _CONTROL_RE.search(raw):
        raise CatalogValidationError("archive path contains unsafe characters")
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise CatalogValidationError("archive path must not be absolute")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise CatalogValidationError("archive path contains unsafe components")
    if relative and raw.startswith("/"):
        raise CatalogValidationError("archive media path must be relative")
    return path.as_posix()


def require_sha256(value: object, *, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not _HEX64_RE.fullmatch(digest):
        raise CatalogValidationError(f"{label} must be a 64-character sha256")
    return digest
