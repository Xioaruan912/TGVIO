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
        order: str = "media_id",
        limit: int = 1000,
        offset: int = 0,
    ) -> list[str]: ...


class CatalogSyncServicePort(Protocol):
    async def sync_once(self) -> CatalogSyncResult: ...
