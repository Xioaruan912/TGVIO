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

    async def move(self, source: str, target: str, *, overwrite: bool) -> None: ...


class ArchiveCatalogSource(Protocol):
    async def discover(self) -> ArchiveDiscovery: ...


class PlayerCatalogRepository(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...

    async def apply_package(self, package: CatalogPackage) -> None: ...

    async def deactivate_packages_not_seen(
        self,
        package_ids: set[str],
    ) -> int: ...

    async def refresh_media_activity(self) -> None: ...

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

    async def finish_favorite_sync(
        self, job_id: int, status: Literal["retry", "synced", "failed"],
        error_code: str | None, *, retry_at: int | None = None,
    ) -> None: ...

    async def save_favorite_location(
        self, media_id: str, relpath: str, size_bytes: int, mime_type: str
    ) -> None: ...

    async def list_favorite_locations(
        self,
    ) -> list[tuple[str, str, int, str]]: ...


class CatalogSyncServicePort(Protocol):
    async def sync_once(self) -> CatalogSyncResult: ...
