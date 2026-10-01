"""Bounded read-only WebDAV discovery, including packages absent from the Bot DB."""
from __future__ import annotations

import asyncio
import hashlib
import re
import urllib.parse
import xml.etree.ElementTree as ET

from tgvio.application.rendition_backfill import canonical
from tgvio.domain.renditions import RenditionTask, safe_path
from tgvio.infrastructure.archive_retry import retry_archive


class RenditionDiscovery:
    def __init__(self, transport, remote_root: str):
        self.transport = transport
        self.root = safe_path(remote_root)

    async def collections(self, path: str) -> tuple[str, ...]:
        return await retry_archive(lambda: asyncio.to_thread(self._collections, path))

    def _collections(self, path: str) -> tuple[str, ...]:
        conn = self.transport._connect()
        absolute = self.transport._absolute_path(path).rstrip("/") + "/"
        try:
            conn.request("PROPFIND", self.transport._quote_path(absolute),
                         body=b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
                         headers={"Authorization": self.transport._authorization,
                                  "Depth": "1", "Content-Type": "application/xml"})
            response = conn.getresponse()
            if response.status not in (200, 207):
                raise RuntimeError("archive discovery unavailable")
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise ValueError("archive discovery exceeds metadata budget")
            root = ET.fromstring(payload)
            names = set()
            for row in root.findall("{DAV:}response"):
                if row.find(".//{DAV:}collection") is None:
                    continue
                href = row.findtext("{DAV:}href") or ""
                decoded = urllib.parse.unquote(urllib.parse.urlsplit(href).path).rstrip("/")
                if not decoded.startswith(absolute):
                    continue
                name = decoded[len(absolute):]
                if name and "/" not in name and not name.startswith("."):
                    names.add(safe_path(name))
            return tuple(sorted(names))
        finally:
            conn.close()

    async def tasks(self) -> list[RenditionTask]:
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
                marker = await self.transport.read_json(f"{path}/_COMPLETE.json")
                if marker is None:
                    continue
                manifest = await self.transport.read_json(f"{path}/manifest.json")
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
        tasks.sort(key=lambda t: (float(t.media.get("size_bytes") or 0)
                                  / max(1, float(t.media.get("duration_seconds") or 0))), reverse=True)
        return tasks
