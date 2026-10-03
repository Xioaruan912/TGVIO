"""Bounded, resumable cover backfill over already committed archive packages.

A committed package is never rewritten: ``manifest.json`` and ``_COMPLETE.json``
stay byte-identical (their hash is what the archive verifies), and covers are
added only as sidecar objects plus a ``covers.json`` index written last.

The runner is pure orchestration over injected ports - list packages, read one
package-relative object, write one package-relative object - so it is tested
offline and the transport wiring stays mechanical.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Mapping, Protocol, Sequence
import json

from tgvio.domain.renditions import RenditionTask, SampledFrame, canonical, safe_path
from tgvio.application.cover_backfill import (
    COVERS_INDEX_NAME,
    COVERS_SCHEMA,
    CoverBackfill,
    parse_covers_index,
    plan_sidecar_writes,
    targets_from_manifest,
)

DEFAULT_INDEX_MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class PackageRef:
    """One committed package: its identity, remote root and decoded manifest."""

    package_id: str
    remote_path: str
    manifest: Mapping[str, object]


class BackfillPort(Protocol):
    async def packages(self, limit: int | None) -> Sequence[PackageRef]: ...

    async def read(self, relpath: str, *, max_bytes: int) -> bytes | None: ...

    async def write(self, relpath: str, payload: bytes) -> None: ...


@dataclass
class BackfillRun:
    seen: int = 0
    skipped: int = 0
    updated: int = 0
    covers: int = 0
    failures: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "packages_seen": self.seen,
            "packages_skipped": self.skipped,
            "packages_updated": self.updated,
            "covers_written": self.covers,
            "failures": self.failures,
        }


def index_is_present(payload: bytes | None) -> bool:
    """An index that parses under our schema means this package was processed.

    Including the case where the package produced no cover at all, so a resumed
    run does not sample the same videos again.
    """
    if not payload:
        return False
    try:
        document = json.loads(payload)
    except (ValueError, TypeError):
        return False
    return isinstance(document, Mapping) and document.get("schema") == COVERS_SCHEMA


class CoverBackfillRunner:
    def __init__(
        self,
        port: BackfillPort,
        backfill: CoverBackfill,
        *,
        limit: int | None = None,
        dry_run: bool = False,
        index_max_bytes: int = DEFAULT_INDEX_MAX_BYTES,
    ) -> None:
        self._port = port
        self._backfill = backfill
        self._limit = limit
        self._dry_run = dry_run
        self._index_max_bytes = max(1, int(index_max_bytes))

    async def run(self) -> BackfillRun:
        run = BackfillRun()
        for package in await self._port.packages(self._limit):
            run.seen += 1
            if index_is_present(await self._read_index(package)):
                run.skipped += 1
                continue
            targets = targets_from_manifest(package.manifest)
            if not targets:
                run.skipped += 1
                continue
            result = await self._backfill.run(targets)
            writes = plan_sidecar_writes(package.remote_path, result)
            if not writes:
                run.skipped += 1
                continue
            if self._dry_run:
                run.updated += 1
                run.covers += len(result.covers)
                continue
            try:
                # Covers first, index last: a reader never sees an index pointing
                # at a cover that is not uploaded yet.
                for write in writes:
                    await self._port.write(write.relpath, write.payload)
            except Exception:
                # A partly written package is only missing covers; the archive
                # stays valid and the next run resumes from the same place.
                run.failures += 1
                continue
            run.updated += 1
            run.covers += len(result.covers)
        return run

    async def _read_index(self, package: PackageRef) -> bytes | None:
        relpath = f"{package.remote_path.rstrip('/')}/{COVERS_INDEX_NAME}"
        try:
            return await self._port.read(relpath, max_bytes=self._index_max_bytes)
        except Exception:
            return None


class CommittedCoverPort(Protocol):
    async def read_json(self, path: str): ...
    async def exists(self, path: str, size: int) -> bool: ...
    async def sample(self, path: str, size: int, work: Path) -> SampledFrame | None: ...
    async def write_cover(self, path: str, payload: bytes) -> None: ...
    async def write_json(self, path: str, value) -> None: ...


def _is_phash(value: object) -> bool:
    """16 lowercase hex digits, and nothing else: what the Player is willing to store."""
    return (isinstance(value, str) and len(value) == 16
            and all(character in "0123456789abcdef" for character in value))


class CommittedCoverBackfill:
    """Single-writer v2 projection; never rewrites committed manifest or marker."""

    def __init__(self, port: CommittedCoverPort) -> None:
        self.port = port

    async def run(self, task: RenditionTask, work: Path) -> int:
        root = safe_path(task.root)
        parent = task.media["sha256"]
        source = safe_path(task.media["path"])
        await self._unchanged(task)
        index = await self.port.read_json(f"{root}/{COVERS_INDEX_NAME}")
        if index is None:
            index = {"schema": "tgvio.archive.covers/v2", "package_id": task.package_id,
                     "manifest_sha256": task.manifest_hash, "algorithm": "bounded-frame-v2",
                     "covers": {}}
        if (not isinstance(index, dict) or index.get("schema") != "tgvio.archive.covers/v2"
                or index.get("package_id") != task.package_id
                or index.get("manifest_sha256") != task.manifest_hash
                or index.get("algorithm") != "bounded-frame-v2"
                or not isinstance(index.get("covers"), dict)):
            raise ValueError("cover index conflict")
        existing = index["covers"].get(source)
        verified_digest: str | None = None
        if isinstance(existing, dict) and existing.get("media_sha256") == parent:
            digest = str(existing.get("sha256", ""))
            path = existing.get("path")
            size = existing.get("size_bytes")
            if (len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
                    and path == f"cover/backfill/{digest}.jpg"
                    and type(size) is int and 0 < size <= 1_000_000
                    and existing.get("mime_type") == "image/jpeg"
                    and await self.port.exists(f"{root}/{path}", size)):
                # A verified cover that predates the fingerprint is not finished work:
                # adding the hash is what this run is for. A verified cover that already
                # carries one is finished, and must not cost a decode.
                if _is_phash(existing.get("phash")):
                    return 0
                verified_digest = digest
        if not await self.port.exists(f"{root}/{source}", int(task.media["size_bytes"])):
            raise ValueError("cover source removed")
        payload = await self.port.sample(f"{root}/{source}", int(task.media["size_bytes"]), work)
        if (payload is None or not 0 < len(payload.payload) <= 1_000_000
                or not payload.payload.startswith(b"\xff\xd8")
                or not payload.payload.endswith(b"\xff\xd9")):
            raise ValueError("no usable frame within cover budget")
        image, phash = payload.payload, payload.phash
        digest = hashlib.sha256(image).hexdigest()
        path = f"cover/backfill/{digest}.jpg"
        if verified_digest == digest:
            # The same frame as the verified cover: the image stays where it is and only
            # the index learns the fingerprint.
            index["covers"][source] = {**existing, "phash": phash}
            if len(canonical(index)) > 512 * 1024:
                raise ValueError("cover index exceeds reader budget")
            await self._unchanged(task)
            if not await self.port.exists(f"{root}/{source}", int(task.media["size_bytes"])):
                raise ValueError("cover source removed before index commit")
            await self.port.write_json(f"{root}/{COVERS_INDEX_NAME}", index)
            return 1
        await self._unchanged(task)
        if not await self.port.exists(f"{root}/{source}", int(task.media["size_bytes"])):
            raise ValueError("cover source removed during sample")
        await self.port.write_cover(f"{root}/{path}", image)
        index["covers"][source] = {"path": path, "size_bytes": len(image),
                                   "media_sha256": parent, "sha256": digest,
                                   "mime_type": "image/jpeg", "phash": phash}
        if len(canonical(index)) > 512 * 1024:
            raise ValueError("cover index exceeds reader budget")
        await self._unchanged(task)
        if not await self.port.exists(f"{root}/{source}", int(task.media["size_bytes"])):
            raise ValueError("cover source removed before index commit")
        # Verified image first, bound index last. Other media's entries survive.
        await self.port.write_json(f"{root}/{COVERS_INDEX_NAME}", index)
        return 1

    async def _unchanged(self, task: RenditionTask) -> None:
        manifest = await self.port.read_json(f"{task.root}/manifest.json")
        marker = await self.port.read_json(f"{task.root}/_COMPLETE.json")
        if (not isinstance(manifest, dict) or not isinstance(marker, dict)
                or manifest.get("package_id") != task.package_id
                or marker.get("package_id") != task.package_id
                or hashlib.sha256(canonical(manifest)).hexdigest() != task.manifest_hash
                or marker.get("manifest_sha256") != task.manifest_hash
                or task.media not in manifest.get("media", [])):
            raise ValueError("committed cover source changed")
