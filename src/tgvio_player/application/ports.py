from __future__ import annotations

from typing import Protocol

from tgvio_player.domain.catalog import (
    ArchiveDiscovery,
    CatalogPackage,
    CatalogSyncResult,
)


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


class CatalogSyncServicePort(Protocol):
    async def sync_once(self) -> CatalogSyncResult: ...
