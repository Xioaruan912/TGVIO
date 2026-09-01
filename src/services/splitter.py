"""Streaming, verifiable file volumes for Telegram's per-file upload limit."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..security import sanitize_filename


class SplitStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class SplitBundle:
    manifest_path: str
    parts: tuple[str, ...]
    original_name: str
    original_sha256: str


def create_split_bundle(source: str, workdir: str, part_bytes: int) -> SplitBundle:
    """Copy *source* into bounded volumes and an atomically-written manifest.

    Parts are intentionally generic documents, not fake video clips: every byte
    is preserved and the manifest gives the recipient an independent checksum.
    """
    source_path = Path(source).resolve()
    root = Path(workdir).resolve()
    if not source_path.is_file() or part_bytes < 1:
        raise ValueError("invalid split source or part size")
    root.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(source_path.name) or "media.bin"
    target = (root / f"split-{hashlib.sha256(str(source_path).encode()).hexdigest()[:12]}").resolve()
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise ValueError("split output escapes managed workdir")
    target.mkdir(exist_ok=True)
    size = source_path.stat().st_size
    free = os.statvfs(target).f_bavail * os.statvfs(target).f_frsize
    if free < size + 16 * 1024 * 1024:
        raise SplitStorageError("insufficient free disk space for safe split volumes")

    digest = hashlib.sha256()
    parts: list[dict[str, object]] = []
    paths: list[str] = []
    index = 0
    with source_path.open("rb") as source_file:
        while True:
            first = source_file.read(1)
            if not first:
                break
            index += 1
            filename = f"{safe_name}.part{index:03d}"
            final = target / filename
            temporary = target / f".{filename}.tmp"
            part_hash = hashlib.sha256()
            written = 0
            with temporary.open("wb") as out:
                out.write(first)
                digest.update(first)
                part_hash.update(first)
                written = 1
                while written < part_bytes:
                    chunk = source_file.read(min(1024 * 1024, part_bytes - written))
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    part_hash.update(chunk)
                    written += len(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, final)
            paths.append(str(final))
            parts.append({"name": filename, "size_bytes": written, "sha256": part_hash.hexdigest()})
    if not paths:
        raise ValueError("cannot split empty file")
    manifest = {
        "schema_version": 1,
        "original_name": safe_name,
        "original_size_bytes": size,
        "original_sha256": digest.hexdigest(),
        "part_count": len(parts),
        "parts": parts,
        "reassemble": f"cat {safe_name}.part* > {safe_name}",
    }
    manifest_path = target / f"{safe_name}.parts.json"
    temporary_manifest = target / f".{manifest_path.name}.tmp"
    with temporary_manifest.open("w", encoding="utf-8") as out:
        json.dump(manifest, out, ensure_ascii=False, separators=(",", ":"))
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary_manifest, manifest_path)
    return SplitBundle(str(manifest_path), tuple(paths), safe_name, digest.hexdigest())
