from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from typing import Any


class ArchivePackageState(StrEnum):
    PLANNED = "planned"
    STAGING = "staging"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    COMMITTED = "committed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArchiveObjectState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    STORED = "stored"
    FAILED = "failed"


class ArchiveObjectRole(StrEnum):
    MEDIA = "media"


ARCHIVE_PACKAGE_TRANSITIONS: dict[ArchivePackageState, set[ArchivePackageState]] = {
    ArchivePackageState.PLANNED: {
        ArchivePackageState.STAGING,
        ArchivePackageState.FAILED,
        ArchivePackageState.CANCELLED,
    },
    ArchivePackageState.STAGING: {
        ArchivePackageState.UPLOADING,
        ArchivePackageState.FAILED,
        ArchivePackageState.CANCELLED,
    },
    ArchivePackageState.UPLOADING: {
        ArchivePackageState.VERIFYING,
        ArchivePackageState.FAILED,
        ArchivePackageState.CANCELLED,
    },
    ArchivePackageState.VERIFYING: {
        ArchivePackageState.COMMITTED,
        ArchivePackageState.FAILED,
        ArchivePackageState.CANCELLED,
    },
    ArchivePackageState.FAILED: {
        ArchivePackageState.STAGING,
        ArchivePackageState.CANCELLED,
    },
    ArchivePackageState.COMMITTED: set(),
    ArchivePackageState.CANCELLED: set(),
}


ARCHIVE_OBJECT_TRANSITIONS: dict[ArchiveObjectState, set[ArchiveObjectState]] = {
    ArchiveObjectState.PENDING: {
        ArchiveObjectState.UPLOADING,
        ArchiveObjectState.FAILED,
    },
    ArchiveObjectState.UPLOADING: {
        ArchiveObjectState.VERIFYING,
        ArchiveObjectState.STORED,
        ArchiveObjectState.FAILED,
    },
    ArchiveObjectState.VERIFYING: {
        ArchiveObjectState.STORED,
        ArchiveObjectState.FAILED,
    },
    ArchiveObjectState.FAILED: {ArchiveObjectState.UPLOADING},
    # A stored remote object may later be found missing/corrupt during durable
    # recovery. Re-uploading that exact canonical object is safe and is the
    # only allowed transition out of STORED.
    ArchiveObjectState.STORED: {ArchiveObjectState.UPLOADING},
}


@dataclass(frozen=True, slots=True)
class ArchiveCapabilities:
    """Read-only transport facts discovered before archive execution."""

    supports_propfind: bool
    supports_mkcol: bool
    supports_put: bool
    supports_move: bool
    supports_get: bool
    supports_etag: bool = False
    supports_quota: bool = False
    quota_used_bytes: int | None = None
    quota_available_bytes: int | None = None

    @property
    def commit_mode(self) -> str:
        return "move" if self.supports_move else "complete_marker"


@dataclass(frozen=True, slots=True)
class ArchiveRemoteStat:
    exists: bool
    size_bytes: int | None = None
    etag: str | None = None
    is_collection: bool = False


@dataclass(frozen=True, slots=True)
class ArchiveStoreReceipt:
    remote_path: str
    size_bytes: int
    verification_method: str
    etag: str | None = None
    reused_remote: bool = False


@dataclass(frozen=True, slots=True)
class ArchiveObject:
    package_id: str
    object_index: int
    item_index: int
    role: ArchiveObjectRole
    local_path: str
    remote_relpath: str
    size_bytes: int
    sha256: str
    state: ArchiveObjectState = ArchiveObjectState.PENDING
    id: int | None = None
    verification_method: str | None = None
    remote_etag: str | None = None
    retry_count: int = 0
    next_retry_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.object_index < 0:
            raise ValueError("archive object index must be >= 0")
        if self.item_index < 0:
            raise ValueError("archive item index must be >= 0")
        if self.size_bytes < 0:
            raise ValueError("archive object size must be >= 0")
        if not self.remote_relpath or self.remote_relpath.startswith("/"):
            raise ValueError("archive object path must be relative")
        if not self.sha256:
            raise ValueError("archive object requires sha256")


@dataclass(frozen=True, slots=True)
class ArchivePackage:
    id: str
    job_id: str
    layout_version: str
    remote_path: str
    staging_path: str
    state: ArchivePackageState
    manifest: dict[str, Any]
    objects: tuple[ArchiveObject, ...]
    manifest_sha256: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    committed_at: str | None = None


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    package: ArchivePackage
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArchiveEvent:
    id: int | None
    package_id: str
    event_type: str
    object_id: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None


def archive_json_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical JSON encoding used by manifest and commit-marker hashes."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def archive_json_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(archive_json_bytes(payload)).hexdigest()


def archive_complete_marker(package: ArchivePackage) -> dict[str, Any]:
    """Deterministic final commit marker; it contains no runtime credentials."""

    return {
        "schema": "tgvio.archive.complete/v1",
        "package_id": package.id,
        "manifest_sha256": package.manifest_sha256 or archive_json_sha256(package.manifest),
        "media_count": len(package.objects),
        "total_bytes": sum(obj.size_bytes for obj in package.objects),
    }
