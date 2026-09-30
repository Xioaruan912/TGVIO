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
from typing import Mapping, Protocol, Sequence
import json

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
