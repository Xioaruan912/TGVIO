"""Validate optional rendition metadata after validating the immutable manifest."""
from __future__ import annotations

import math
from typing import Any

from tgvio_player.domain.catalog import (
    CatalogLocation, CatalogMedia, CatalogPackage, require_sha256, safe_remote_path,
)


def parse_renditions(package: CatalogPackage, document: Any) -> tuple[tuple[CatalogMedia, CatalogLocation], ...]:
    if (not isinstance(document, dict)
            or document.get("schema") != "tgvio.archive.renditions/v1"
            or document.get("package_id") != package.package_id
            or document.get("manifest_sha256") != package.manifest_sha256
            or document.get("algorithm") != "h264-height-crf24-v1"):
        return ()
    entries = document.get("media")
    if not isinstance(entries, list) or len(entries) > 4000:
        return ()
    originals = {m.media_id: m for m in package.media if m.kind == "video" and not m.is_rendition}
    used = {m.media_id for m in package.media}
    paths = {loc.remote_relpath for loc in package.locations}
    slots: set[tuple[str, int]] = set()
    result = []
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                continue
            parent = require_sha256(entry.get("variant_of"), label="parent")
            original = originals.get(parent)
            digest = require_sha256(entry.get("sha256"), label="rendition")
            path = safe_remote_path(entry.get("path"), relative=True)
            height, width = entry.get("height"), entry.get("width")
            size, bitrate = entry.get("size_bytes"), entry.get("bitrate_bps")
            duration = entry.get("duration_seconds")
            if (original is None or digest in used or path in paths
                    or not path.startswith("renditions/") or "%" in path
                    or type(height) is not int or height not in (480, 720)
                    or type(width) is not int or not 2 <= width <= 4096 or width % 2
                    or type(size) is not int or not 0 < size <= 4 * 1024**3
                    or type(bitrate) is not int or not 0 < bitrate <= 10_000_000
                    or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
                    or entry.get("mime_type") != "video/mp4" or entry.get("codec") != "h264"
                    or entry.get("resolution_label") != f"{height}p"
                    or (original.height is not None and height >= original.height)
                    or (parent, height) in slots):
                continue
            if original.duration_seconds and abs(duration - original.duration_seconds) > max(1, original.duration_seconds * .01):
                continue
            media = CatalogMedia(digest, "video", size, "video/mp4", width, height,
                                 float(duration), "mp4", "h264", parent, f"{height}p", bitrate)
            result.append((media, CatalogLocation(digest, package.package_id, path)))
            slots.add((parent, height))
            used.add(digest)
            paths.add(path)
        except (TypeError, ValueError, OverflowError):
            continue
    return tuple(result)
