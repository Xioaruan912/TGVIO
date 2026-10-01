"""Validate additive stills against the already validated immutable package."""
from __future__ import annotations

from typing import Any
from tgvio_player.domain.catalog import (
    CatalogCover, CatalogPackage, MAX_COVER_BYTES, require_sha256, safe_remote_path,
)


def parse_covers(package: CatalogPackage, document: Any) -> tuple[CatalogCover, ...]:
    if (not isinstance(document, dict)
            or document.get("schema") != "tgvio.archive.covers/v2"
            or document.get("package_id") != package.package_id
            or document.get("manifest_sha256") != package.manifest_sha256
            or document.get("algorithm") != "bounded-frame-v2"):
        return ()
    entries = document.get("covers")
    if not isinstance(entries, dict) or len(entries) > 4000:
        return ()
    originals = {m.media_id: m for m in package.media if m.kind == "video" and not m.is_rendition}
    paths = {loc.remote_relpath: loc.media_id for loc in package.locations}
    covered = {c.media_id for c in package.covers}
    result = []
    for source, entry in entries.items():
        try:
            if not isinstance(entry, dict):
                continue
            parent = require_sha256(entry.get("media_sha256"), label="cover parent")
            digest = require_sha256(entry.get("sha256"), label="cover hash")
            path = safe_remote_path(entry.get("path"), relative=True)
            size = entry.get("size_bytes")
            if (parent not in originals or paths.get(source) != parent or parent in covered
                    or path != f"cover/backfill/{digest}.jpg"
                    or type(size) is not int or not 0 < size <= MAX_COVER_BYTES
                    or entry.get("mime_type") != "image/jpeg"):
                continue
            result.append(CatalogCover(parent, package.package_id, path, size, "image/jpeg", "bounded-frame-v2"))
            covered.add(parent)
        except (TypeError, ValueError):
            continue
    return tuple(result)
