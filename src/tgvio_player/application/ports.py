from __future__ import annotations

from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Literal, Protocol

from tgvio_player.domain.catalog import (
    ArchiveDiscovery,
    CatalogPackage,
    CatalogSyncResult,
)
from tgvio_player.domain.storage_settings import PlayerStorageSettings


@dataclass(frozen=True, slots=True)
class FavoriteSyncJob:
    job_id: int
    media_id: str
    operation: Literal["upload", "delete"]
    status: Literal["pending", "running", "retry", "synced", "failed"]
    attempts: int
    error_code: str | None
    intent_persisted: bool = False


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    status_code: int
    size_bytes: int
    etag: str | None


@dataclass(frozen=True, slots=True)
class DeleteReceipt:
    status_code: int
    deleted: bool


@dataclass(frozen=True, slots=True)
class RemoteFileStat:
    size_bytes: int
    etag: str | None


class WebDavWriteClient(Protocol):
    async def ensure_directory(self, path: str) -> None: ...

    async def put_stream(
        self, path: str, chunks: AsyncIterator[bytes], *, size_bytes: int, content_type: str
    ) -> UploadReceipt: ...

    async def delete(self, path: str) -> DeleteReceipt: ...

    async def stat(self, path: str) -> RemoteFileStat | None: ...

    async def get_bytes(self, path: str, *, max_bytes: int) -> bytes | None: ...

    async def open_stream(self, path: str) -> tuple[int, str | None, AsyncIterator[bytes]]: ...

    async def open_range(self, path: str, byte_range: object | None): ...

    async def move(self, source: str, target: str, *, overwrite: bool) -> None: ...


class PlayerStateCipherPort(Protocol):
    def encrypt(self, payload: bytes, *, context: bytes) -> bytes: ...

    def decrypt(self, envelope: bytes, *, context: bytes) -> bytes: ...


class WebDavWriteError(RuntimeError):
    def __init__(self, operation: str, category: str, status_code: int | None = None) -> None:
        self.operation = operation
        self.category = category
        self.status_code = status_code
        suffix = f" ({status_code})" if status_code is not None else ""
        super().__init__(f"WebDAV {operation} failed: {category}{suffix}")


class ArchiveCatalogSource(Protocol):
    async def discover(self) -> ArchiveDiscovery: ...


class PlayerCatalogRepository(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...

    async def apply_package(self, package: CatalogPackage) -> None: ...

    async def active_cover(self, media_id: str) -> dict[str, object] | None: ...

    async def active_cover_records(self, media_id: str) -> list[tuple[str, str, str]]: ...

    async def record_deleted_cover(self, media_id: str, package_id: str) -> None: ...

    async def deactivate_packages_not_seen(
        self,
        package_ids: set[str],
    ) -> int: ...

    async def refresh_media_activity(self) -> None: ...

    async def favorite_media_id_for_archive_location(
        self, package_path: str, remote_relpath: str
    ) -> str | None: ...

    async def count_active_videos(self) -> int: ...

    async def list_active_video_ids(self, *, limit: int = 1000) -> list[str]: ...

    async def list_video_ids(
        self,
        *,
        min_seconds: float | None = None,
        max_seconds: float | None = None,
        media_id_prefix: str | None = None,
        order: str = "media_id",
        limit: int = 1000,
        offset: int = 0,
    ) -> list[str]: ...

    async def count_video_ids(
        self,
        *,
        min_seconds: float | None = None,
        max_seconds: float | None = None,
        media_id_prefix: str | None = None,
    ) -> int: ...

    async def library_dates(self) -> dict[str, object]: ...

    async def library_folders(
        self, *, date_filter: str | None = None, media_id: str | None = None
    ) -> dict[str, object]: ...

    async def library_video_page(
        self, folder_id: str, *, category: str = "all", limit: int = 20,
        cursor: str | None = None, long_seconds: float = 300
    ) -> dict[str, object] | None: ...

    async def list_media_groups(self, media_id: str) -> list[tuple[str, str]]: ...

    async def resolve_archive_group(self, group_id: str) -> str | None: ...

    async def list_group_video_ids(
        self, group_id: str, *, after_id: str | None, limit: int
    ) -> list[str]: ...

    async def list_favorite_page(
        self,
        token_digest: str,
        *,
        limit: int,
        before: tuple[int, str] | None,
    ) -> list[tuple[str, int]]: ...

    async def list_long_video_progress(self) -> list[tuple[str, float]]: ...

    async def save_long_video_progress(
        self, media_id: str, position_seconds: float
    ) -> None: ...

    async def delete_long_video_progress(self, media_id: str) -> None: ...

    async def get_storage_settings(self) -> PlayerStorageSettings: ...

    async def save_storage_settings(self, settings: PlayerStorageSettings) -> None: ...

    async def set_global_favorite(self, media_id: str, enabled: bool) -> None: ...

    async def list_global_favorite_page(
        self, *, limit: int, before: tuple[int, str] | None
    ) -> list[tuple[str, int]]: ...

    async def enqueue_favorite_sync(
        self, media_id: str, operation: Literal["upload", "delete"]
    ) -> None: ...

    async def claim_favorite_sync(self, *, limit: int) -> list[FavoriteSyncJob]: ...

    async def list_pending_favorite_sync(self) -> list[FavoriteSyncJob]: ...

    async def is_global_favorite(self, media_id: str) -> bool: ...

    async def get_favorite_location(
        self, media_id: str
    ) -> tuple[str, int, str] | None: ...

    async def delete_favorite_location(self, media_id: str) -> None: ...

    async def favorite_sync_summary(self) -> dict[str, int | None]: ...

    async def recover_interrupted_favorite_sync(self) -> None: ...

    async def retry_failed_favorite_sync(self) -> int: ...

    async def mark_favorite_delete_intent(self, media_id: str) -> None: ...

    async def finish_favorite_sync(
        self, job_id: int, status: Literal["retry", "synced", "failed"],
        error_code: str | None, *, retry_at: int | None = None,
    ) -> None: ...

    async def save_favorite_location(
        self, media_id: str, relpath: str, size_bytes: int, mime_type: str
    ) -> None: ...

    async def restore_favorite_copy(
        self, media_id: str, relpath: str, size_bytes: int, mime_type: str, created_at: int
    ) -> None: ...

    async def restore_pending_favorite(self, media_id: str, created_at: int) -> None: ...

    async def list_favorite_locations(
        self,
    ) -> list[tuple[str, str, int, str]]: ...

    async def active_media_details(self, media_id: str) -> dict[str, object] | None: ...

    async def active_variants(self, media_id: str) -> list[dict[str, object]]: ...


class CatalogSyncServicePort(Protocol):
    async def sync_once(self) -> CatalogSyncResult: ...
