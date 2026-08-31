"""D1 content hashing and destination-scoped Telegram media index service."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from typing import Any

from telethon import types
from telethon.utils import get_input_document, get_input_photo, get_peer_id


_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ContentHash:
    path: str
    sha256: str
    size_bytes: int
    md5_short: str = ""


def sha256_file(path: str, *, chunk_size: int = _HASH_CHUNK) -> ContentHash:
    """Hash one file with bounded memory, reusing the pass for WebDAV MD5 naming."""
    digest = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(max(4096, int(chunk_size)))
            if not chunk:
                break
            digest.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return ContentHash(
        path=os.path.realpath(path),
        sha256=digest.hexdigest(),
        size_bytes=size,
        md5_short=md5.hexdigest()[:8],
    )


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

    @staticmethod
    def media_kind(path: str) -> str:
        lower = str(path).lower()
        if lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return "photo"
        return "document"

    async def reuse_input_media(
        self,
        client: Any,
        content: ContentHash,
        *,
        spoiler: bool = False,
        destination_key: str | None = None,
    ):
        entry = await self.lookup(
            sha256=content.sha256,
            size_bytes=content.size_bytes,
            media_kind=self.media_kind(content.path),
            destination_key=destination_key,
        )
        if entry is None:
            return None, None
        try:
            message = await client.get_messages(entry.source_peer_id, ids=entry.source_message_id)
            media = getattr(message, "media", None)
            if isinstance(media, types.MessageMediaPhoto) and getattr(media, "photo", None) is not None:
                return types.InputMediaPhoto(id=get_input_photo(media.photo), spoiler=spoiler or None), entry
            if isinstance(media, types.MessageMediaDocument) and getattr(media, "document", None) is not None:
                return types.InputMediaDocument(
                    id=get_input_document(media.document), spoiler=spoiler or None
                ), entry
        except Exception:
            pass
        # Stale destination message / file reference / permissions: remove only
        # this optimization entry and let normal upload continue.
        await self.repository.delete_dedup_entry(entry.id)
        return None, None

    async def record_sent_message(
        self,
        content: ContentHash,
        message: Any,
        *,
        destination_key: str | None = None,
    ) -> None:
        media = getattr(message, "media", None)
        peer_id = 0
        try:
            peer_id = int(get_peer_id(getattr(message, "peer_id", None)))
        except Exception:
            return
        if not peer_id or not getattr(message, "id", None):
            return
        media_id = access_hash = None
        file_reference = None
        if isinstance(media, types.MessageMediaPhoto) and getattr(media, "photo", None) is not None:
            obj = media.photo
            media_id = getattr(obj, "id", None)
            access_hash = getattr(obj, "access_hash", None)
            file_reference = getattr(obj, "file_reference", None)
            kind = "photo"
        elif isinstance(media, types.MessageMediaDocument) and getattr(media, "document", None) is not None:
            obj = media.document
            media_id = getattr(obj, "id", None)
            access_hash = getattr(obj, "access_hash", None)
            file_reference = getattr(obj, "file_reference", None)
            kind = "document"
        else:
            return
        await self.repository.upsert_dedup_entry(
            sha256=content.sha256,
            size_bytes=content.size_bytes,
            media_kind=kind,
            destination_key=str(destination_key or self.destination_key),
            source_peer_id=peer_id,
            source_message_id=int(message.id),
            media_id=media_id,
            access_hash=access_hash,
            file_reference=file_reference,
            metadata={"schema_version": 1},
        )

    async def mark_hit(self, entry: Any, content: ContentHash) -> None:
        await self.repository.mark_dedup_hit(entry.id, saved_bytes=content.size_bytes)

    async def lookup(
        self,
        *,
        sha256: str,
        size_bytes: int,
        media_kind: str,
        destination_key: str | None = None,
    ):
        return await self.repository.lookup_dedup_entry(
            sha256=str(sha256),
            size_bytes=int(size_bytes),
            media_kind=str(media_kind),
            destination_key=str(destination_key or self.destination_key),
        )
