"""Bounded read-only WebDAV discovery, including packages absent from the Bot DB."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import urllib.parse
import xml.etree.ElementTree as ET

from tgvio.application.rendition_backfill import canonical
from tgvio.domain.renditions import RenditionTask, safe_path
from tgvio.infrastructure.archive_retry import retry_archive


_PROPFIND = (b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/>'
             b'<d:getetag/></d:prop></d:propfind>')


class RenditionDiscovery:
    """Package metadata is read again only when its listed ETag changes.

    Every GET through OpenList costs a download-link call on the cloud drive; a
    full re-read of every package on each scan kept that budget under constant
    pressure. The package listing that carries the ETags comes from OpenList's
    directory cache instead.
    """

    def __init__(self, transport, remote_root: str):
        self.transport = transport
        self.root = safe_path(remote_root)
        # remote path -> (etag, parsed payload) of the last successful read.
        self._metadata: dict[str, tuple[str, object]] = {}
        self.metadata_reads = 0
        self.metadata_hits = 0

    async def collections(self, path: str) -> tuple[str, ...]:
        return await retry_archive(lambda: asyncio.to_thread(self._collections, path))

    async def files(self, path: str) -> tuple[tuple[str, int], ...]:
        entries = await retry_archive(lambda: asyncio.to_thread(self._entries, path, True))
        return tuple((name, size) for name, collection, size, _etag in entries
                     if not collection and isinstance(size, int) and size > 0)

    def _collections(self, path: str) -> tuple[str, ...]:
        return tuple(name for name, collection, _size, _etag in self._entries(path) if collection)

    def _entries(self, path: str, allow_missing: bool = False
                 ) -> tuple[tuple[str, bool, int | None, str | None], ...]:
        conn = self.transport._connect()
        absolute = self.transport._absolute_path(path).rstrip("/") + "/"
        try:
            conn.request("PROPFIND", self.transport._quote_path(absolute),
                         body=_PROPFIND,
                         headers={"Authorization": self.transport._authorization,
                                  "Depth": "1", "Content-Type": "application/xml"})
            response = conn.getresponse()
            if response.status == 404 and allow_missing:
                return ()
            if response.status not in (200, 207):
                raise RuntimeError("archive discovery unavailable")
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise ValueError("archive discovery exceeds metadata budget")
            root = ET.fromstring(payload)
            names = {}
            for row in root.findall("{DAV:}response"):
                props = [p for p in row.findall("{DAV:}propstat")
                         if re.search(r"\s200(?:\s|$)", p.findtext("{DAV:}status", ""))]
                if not props:
                    continue
                href = row.findtext("{DAV:}href") or ""
                decoded = urllib.parse.unquote(urllib.parse.urlsplit(href).path).rstrip("/")
                if not decoded.startswith(absolute):
                    continue
                name = decoded[len(absolute):]
                if name and "/" not in name and not name.startswith("."):
                    collection = any(p.find(".//{DAV:}collection") is not None for p in props)
                    length = next((p.findtext(".//{DAV:}getcontentlength") for p in props
                                   if p.findtext(".//{DAV:}getcontentlength") is not None), None)
                    try:
                        size = int(length) if length is not None else None
                    except ValueError:
                        size = None
                    etag = next((p.findtext(".//{DAV:}getetag") for p in props
                                 if p.findtext(".//{DAV:}getetag")), None)
                    names[safe_path(name)] = (collection, size, etag)
                    if len(names) > 10000:
                        raise ValueError("archive collection entry budget exceeded")
            return tuple((name, *names[name]) for name in sorted(names))
        finally:
            conn.close()

    async def _etags(self, path: str) -> dict[str, str] | None:
        """ETags of a package's files, or None when the listing is unavailable."""
        try:
            entries = await asyncio.to_thread(self._entries, path, True)
        except Exception:
            # The listing is only a shortcut; reading the files directly still works.
            return None
        return {name: etag for name, collection, _size, etag in entries if not collection and etag}

    async def _read(self, path: str, etags: dict[str, str] | None, seen: set[str]):
        seen.add(path)
        etag = None if etags is None else etags.get(path.rsplit("/", 1)[-1])
        cached = self._metadata.get(path)
        if etag is not None and cached is not None and cached[0] == etag:
            self.metadata_hits += 1
            return copy.deepcopy(cached[1])
        value = await self.transport.read_json(path)
        self.metadata_reads += 1
        if etag is not None and value is not None:
            self._metadata[path] = (etag, copy.deepcopy(value))
        else:
            self._metadata.pop(path, None)
        return value

    async def tasks(self) -> list[RenditionTask]:
        seen: set[str] = set()
        dates = await self.collections(self.root)
        if len(dates) > 2000:
            raise ValueError("archive date budget exceeded")
        tasks = []
        packages = 0
        for date in dates:
            for name in await self.collections(f"{self.root}/{date}"):
                packages += 1
                if packages > 10000:
                    raise ValueError("archive package budget exceeded")
                path = f"{self.root}/{date}/{name}"
                etags = await self._etags(path)
                if etags is not None and "_COMPLETE.json" not in etags:
                    continue  # not committed yet; nothing to read
                marker = await self._read(f"{path}/_COMPLETE.json", etags, seen)
                if marker is None:
                    continue
                manifest = await self._read(f"{path}/manifest.json", etags, seen)
                if (not isinstance(manifest, dict) or not isinstance(marker, dict)
                        or marker.get("schema") != "tgvio.archive.complete/v1"
                        or manifest.get("schema") not in ("tgvio.archive/v1", "tgvio.archive/v2", "tgvio.archive/v3")):
                    continue
                digest = hashlib.sha256(canonical(manifest)).hexdigest()
                ident = manifest.get("package_id")
                media = manifest.get("media")
                if (not ident or ident != marker.get("package_id")
                        or digest != marker.get("manifest_sha256")
                        or not isinstance(media, list)
                        or len(media) != marker.get("media_count")
                        or len(media) != manifest.get("media_count")):
                    continue
                for item in media:
                    if (isinstance(item, dict) and item.get("kind") == "video"
                            and not item.get("variant_of")
                            and re.fullmatch("[0-9a-f]{64}", str(item.get("sha256", "")))):
                        safe_path(item["path"])
                        tasks.append(RenditionTask(ident, path, digest, item))
        # The scan finished: forget metadata of packages that are gone.
        for path in [path for path in self._metadata if path not in seen]:
            del self._metadata[path]
        tasks.sort(key=lambda t: (float(t.media.get("size_bytes") or 0)
                                  / max(1, float(t.media.get("duration_seconds") or 0))), reverse=True)
        return tasks
