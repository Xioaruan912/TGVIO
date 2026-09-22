from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from tgvio_player.domain.catalog import (
    ArchiveDiscovery,
    ArchivePackageCandidate,
    safe_remote_path,
)


_METADATA_LIMIT = 512 * 1024


@dataclass(frozen=True, slots=True)
class WebDavCollectionEntry:
    name: str
    is_collection: bool
    etag: str | None = None


class WebDavCatalogClient(Protocol):
    """Read-only metadata transport required by the R2-19A catalog."""

    async def list_collection(self, remote_path: str) -> tuple[WebDavCollectionEntry, ...]: ...

    async def get_json(self, remote_path: str, *, max_bytes: int) -> Any | None: ...


class WebDavArchiveCatalogSource:
    """Discover only committed Archive packages using bounded metadata requests."""

    def __init__(self, client: WebDavCatalogClient, *, remote_root: str) -> None:
        self._client = client
        self._remote_root = safe_remote_path(remote_root, relative=False)

    async def discover(self) -> ArchiveDiscovery:
        try:
            date_entries = await self._client.list_collection(self._remote_root)
        except Exception:
            return ArchiveDiscovery(packages=(), complete_scan=False)

        packages: list[ArchivePackageCandidate] = []
        complete_scan = True
        for date_entry in date_entries:
            if not date_entry.is_collection:
                continue
            date_path = self._child(self._remote_root, date_entry.name)
            if date_path is None:
                complete_scan = False
                continue
            try:
                package_entries = await self._client.list_collection(date_path)
            except Exception:
                complete_scan = False
                continue
            for package_entry in package_entries:
                if not package_entry.is_collection:
                    continue
                package_path = self._child(date_path, package_entry.name)
                if package_path is None:
                    complete_scan = False
                    continue
                candidate, package_complete = await self._package_candidate(package_path)
                complete_scan = complete_scan and package_complete
                if candidate is not None:
                    packages.append(candidate)
        return ArchiveDiscovery(packages=tuple(packages), complete_scan=complete_scan)

    async def _package_candidate(
        self, package_path: str
    ) -> tuple[ArchivePackageCandidate | None, bool]:
        try:
            entries = await self._client.list_collection(package_path)
        except Exception:
            return None, False
        by_name = {entry.name: entry for entry in entries if not entry.is_collection}
        manifest_entry = by_name.get("manifest.json")
        complete_entry = by_name.get("_COMPLETE.json")
        if manifest_entry is None or complete_entry is None:
            # The package could be mid-commit. Do not treat an incomplete view
            # as proof that an existing committed package was deleted.
            return None, False
        try:
            manifest = await self._client.get_json(
                f"{package_path}/manifest.json", max_bytes=_METADATA_LIMIT
            )
            complete = await self._client.get_json(
                f"{package_path}/_COMPLETE.json", max_bytes=_METADATA_LIMIT
            )
        except Exception:
            return None, False
        return (
            ArchivePackageCandidate(
                remote_path=package_path,
                manifest=manifest,
                complete=complete,
                manifest_etag=manifest_entry.etag,
                complete_etag=complete_entry.etag,
            ),
            True,
        )

    @staticmethod
    def _child(parent: str, name: str) -> str | None:
        try:
            child = safe_remote_path(name, relative=True)
        except ValueError:
            return None
        if "/" in child:
            return None
        return f"{parent}/{child}"
