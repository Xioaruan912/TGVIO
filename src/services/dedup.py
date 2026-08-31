"""D1 content hashing and destination-scoped Telegram media index service."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from typing import Any


_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ContentHash:
    path: str
    sha256: str
    size_bytes: int


def sha256_file(path: str, *, chunk_size: int = _HASH_CHUNK) -> ContentHash:
    """Hash one file with bounded memory."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(max(4096, int(chunk_size)))
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return ContentHash(path=os.path.realpath(path), sha256=digest.hexdigest(), size_bytes=size)


class DedupManager:
    """D1 facade. D1-A only hashes/indexes; it does not change publish behavior."""

    def __init__(self, repository: Any, *, destination_key: str) -> None:
        self.repository = repository
        self.destination_key = str(destination_key)

    async def hash_job_paths(self, legacy_seq: int, paths: str | list[str]) -> list[ContentHash]:
        if self.repository is None:
            return []
        values = list(paths) if isinstance(paths, list) else [paths]
        real_paths = [str(path) for path in values if path and os.path.isfile(path)]
        hashes = await asyncio.gather(*(asyncio.to_thread(sha256_file, path) for path in real_paths))
        if hashes:
            await self.repository.set_job_item_content_hashes(
                int(legacy_seq),
                [(item.sha256, item.size_bytes) for item in hashes],
            )
        return list(hashes)

    async def lookup(self, *, sha256: str, size_bytes: int, media_kind: str):
        return await self.repository.lookup_dedup_entry(
            sha256=str(sha256),
            size_bytes=int(size_bytes),
            media_kind=str(media_kind),
            destination_key=self.destination_key,
        )
