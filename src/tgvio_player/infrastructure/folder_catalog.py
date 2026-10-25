from __future__ import annotations

import hashlib
import json
import re

from tgvio_player.domain.catalog import (
    ArchiveDiscovery,
    CatalogCover,
    CatalogLocation,
    CatalogMedia,
    CatalogPackage,
    safe_remote_path,
)
from tgvio_player.infrastructure.webdav_catalog import (
    WebDavArchiveCatalogSource,
    WebDavCatalogClient,
    WebDavCollectionEntry,
)

_DATE_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}")
_VIDEO_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska",
}
# The backup folder, and folders tools keep for themselves (.staging, _thumbnails).
_SKIPPED_FOLDERS = frozenset({"收藏"})
_MAX_DEPTH = 4
# Low-res copies the player made itself: <library>/.tgvio/<media_id>/480.mp4.
RENDITIONS_DIR = ".tgvio"
_RENDITION_FILE = re.compile(r"(480|720)\.mp4")
_MEDIA_ID = re.compile(r"[0-9a-f]{64}")
COVER_FILE = "cover.jpg"
# cover-<16 hex>.jpg carries the cover's fingerprint for duplicate review.
_COVER_NAME = re.compile(r"cover(?:-([0-9a-f]{16}))?\.jpg")
COVER_ALGORITHM = "player-frame-v1"
_MAX_FOLDERS = 5000


def plain_media_id(size_bytes: int, name: str) -> str:
    """A plain file's id: size and name, not the path, so a moved file keeps its favorites."""
    return hashlib.sha256(f"tgvio-file-v1\n{size_bytes}\n{name}".encode()).hexdigest()


def rendition_media_id(parent_media_id: str, height: int) -> str:
    return hashlib.sha256(f"tgvio-rendition-v1\n{parent_media_id}\n{height}".encode()).hexdigest()


def _skipped(name: str) -> bool:
    return name.startswith((".", "_")) or name in _SKIPPED_FOLDERS


class LibraryCatalogSource:
    """Every configured library folder, whichever shape it has.

    A folder with YYYY-MM-DD subfolders holds Archive packages and is read through
    the package scanner. Any other folder is walked for plain video files; each
    folder that holds videos becomes one catalog package.
    """

    def __init__(self, client: WebDavCatalogClient, *, roots: tuple[str, ...]) -> None:
        self._client = client
        self._packages: dict[str, WebDavArchiveCatalogSource] = {}
        self._roots: tuple[str, ...] = ()
        self.set_roots(roots)

    def set_roots(self, roots: tuple[str, ...]) -> None:
        cleaned = tuple(dict.fromkeys(safe_remote_path(root, relative=False) for root in roots if root))
        self._roots = cleaned
        self._packages = {root: self._packages.get(root) or WebDavArchiveCatalogSource(
            self._client, remote_root=root) for root in cleaned}

    @property
    def metadata_reads(self) -> int:
        return sum(source.metadata_reads for source in self._packages.values())

    @property
    def metadata_hits(self) -> int:
        return sum(source.metadata_hits for source in self._packages.values())

    async def discover(self, *, recent_dates: int | None = None) -> ArchiveDiscovery:
        if not self._roots:
            # Never scan the WebDAV root: a "complete" scan of it would retire every video.
            return ArchiveDiscovery(packages=(), complete_scan=False)
        packages: list = []
        complete = recent_dates is None
        for root in self._roots:
            try:
                entries = await self._client.list_collection(root)
            except Exception:
                complete = False
                continue
            if any(entry.is_collection and _DATE_FOLDER.fullmatch(entry.name) for entry in entries):
                found = await self._packages[root].discover(recent_dates=recent_dates)
            elif recent_dates is not None:
                continue  # plain folders have no "newest dates"; the full scan covers them
            else:
                found = await self._walk(root, entries)
            packages.extend(found.packages)
            complete = complete and found.complete_scan
            if recent_dates is None and any(e.is_collection and e.name == RENDITIONS_DIR for e in entries):
                copies, copies_complete = await self._renditions(root)
                packages.extend(copies)
                complete = complete and copies_complete
        return ArchiveDiscovery(packages=tuple(packages), complete_scan=complete)

    async def _walk(self, root: str, root_entries: tuple[WebDavCollectionEntry, ...]) -> ArchiveDiscovery:
        packages: list[CatalogPackage] = []
        complete = True
        queue: list[tuple[str, int, tuple[WebDavCollectionEntry, ...] | None]] = [(root, 0, root_entries)]
        visited = 0
        while queue:
            path, depth, entries = queue.pop(0)
            visited += 1
            if visited > _MAX_FOLDERS:
                return ArchiveDiscovery(packages=tuple(packages), complete_scan=False)
            if entries is None:
                try:
                    entries = await self._client.list_collection(path)
                except Exception:
                    complete = False
                    continue
            package = self._folder_package(path, entries)
            if package is not None:
                packages.append(package)
            for entry in entries:
                if not entry.is_collection:
                    continue
                try:
                    child = safe_remote_path(entry.name, relative=True)
                except ValueError:
                    complete = False  # an unsafe name means this view cannot be trusted
                    continue
                if "/" in child:
                    complete = False
                    continue
                if _skipped(entry.name):
                    continue
                if depth + 1 <= _MAX_DEPTH:
                    queue.append((f"{path}/{child}", depth + 1, None))
        return ArchiveDiscovery(packages=tuple(packages), complete_scan=complete)

    async def _renditions(self, root: str) -> tuple[list[CatalogPackage], bool]:
        try:
            parents = await self._client.list_collection(f"{root}/{RENDITIONS_DIR}")
        except Exception:
            return [], False
        packages: list[CatalogPackage] = []
        complete = True
        for parent in parents:
            if not parent.is_collection or not _MEDIA_ID.fullmatch(parent.name):
                continue
            path = f"{root}/{RENDITIONS_DIR}/{parent.name}"
            try:
                files = await self._client.list_collection(path)
            except Exception:
                complete = False
                continue
            copies = [(entry, int(entry.name[:3])) for entry in files
                      if not entry.is_collection and entry.size_bytes and _RENDITION_FILE.fullmatch(entry.name)]
            covers = sorted((entry for entry in files
                             if not entry.is_collection and entry.size_bytes and _COVER_NAME.fullmatch(entry.name)),
                            key=lambda entry: entry.name != COVER_FILE, reverse=True)
            cover = covers[0] if covers else None  # a fingerprinted cover wins over a plain one
            if not copies and cover is None:
                continue
            package_id = hashlib.sha256(f"tgvio-folder-v1\n{path}".encode()).hexdigest()
            media = tuple(
                CatalogMedia(rendition_media_id(parent.name, height), "video", entry.size_bytes, "video/mp4",
                             height=height, variant_of=parent.name, label=f"{height}p")
                for entry, height in copies
            )
            listing = sorted((entry.name, entry.size_bytes, entry.etag) for entry, _ in copies)
            if cover is not None:
                listing.append((cover.name, cover.size_bytes, cover.etag))
            packages.append(CatalogPackage(
                package_id=package_id, remote_path=path,
                manifest_sha256=hashlib.sha256(json.dumps(listing).encode()).hexdigest(),
                manifest_etag=None, complete_etag=None, media=media,
                locations=tuple(CatalogLocation(rendition_media_id(parent.name, height), package_id, entry.name, entry.etag)
                                for entry, height in copies),
                covers=() if cover is None else (CatalogCover(
                    parent.name, package_id, cover.name, cover.size_bytes, "image/jpeg", COVER_ALGORITHM,
                    _COVER_NAME.fullmatch(cover.name).group(1)),),
            ))
        return packages, complete

    @staticmethod
    def _folder_package(path: str, entries: tuple[WebDavCollectionEntry, ...]) -> CatalogPackage | None:
        media: list[CatalogMedia] = []
        locations: list[CatalogLocation] = []
        seen: set[str] = set()
        listing: list[tuple[str, int, str | None]] = []
        for entry in entries:
            if entry.is_collection or entry.name.startswith((".", "_")):
                continue
            mime = _VIDEO_TYPES.get(entry.name[entry.name.rfind("."):].lower()) if "." in entry.name else None
            if mime is None or not entry.size_bytes:
                continue
            media_id = plain_media_id(entry.size_bytes, entry.name)
            listing.append((entry.name, entry.size_bytes, entry.etag))
            if media_id not in seen:
                seen.add(media_id)
                media.append(CatalogMedia(media_id, "video", entry.size_bytes, mime))
            locations.append(CatalogLocation(media_id, "", entry.name, entry.etag))
        if not media:
            return None
        package_id = hashlib.sha256(f"tgvio-folder-v1\n{path}".encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(sorted(listing), ensure_ascii=False).encode()).hexdigest()
        return CatalogPackage(
            package_id=package_id, remote_path=path, manifest_sha256=digest,
            manifest_etag=None, complete_etag=None, media=tuple(media),
            locations=tuple(CatalogLocation(item.media_id, package_id, item.remote_relpath, item.remote_etag)
                            for item in locations),
        )
