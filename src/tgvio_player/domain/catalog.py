from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class CatalogValidationError(ValueError):
    """A remote Archive package does not satisfy the Player catalog contract."""


@dataclass(frozen=True, slots=True)
class ArchivePackageCandidate:
    remote_path: str
    manifest: Any
    complete: Any
    manifest_etag: str | None = None
    complete_etag: str | None = None


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
