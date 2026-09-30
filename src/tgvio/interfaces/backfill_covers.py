"""One-off, bounded cover backfill for already committed archive packages.

Run it **inside the Bot container**, so archive credentials never leave the host::

    docker exec tgvio python -m tgvio.interfaces.backfill_covers --dry-run --limit 1

Committed packages are never rewritten: ``manifest.json`` and ``_COMPLETE.json``
keep their bytes (their hash is what the archive verifies) and covers are added
only as sidecar objects with ``covers.json`` written last. A package that already
carries an index is skipped, so repeated runs advance and never redo work.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Sequence

from tgvio.adapters.webdav_archive import WebDavArchiveTransport
from tgvio.application.cover_backfill import CoverBackfill
from tgvio.application.cover_backfill_runner import (
    CoverBackfillRunner,
    PackageRef,
)
from tgvio.config import Settings
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.infrastructure.cover_sampler import HeadSampleCoverPort
from tgvio.infrastructure.media_transformer import FFmpegMediaTransformer
from tgvio.infrastructure.sqlite import SQLiteJobRepository

HEAD_SAMPLE_BYTES = 8 * 1024 * 1024
INDEX_MAX_BYTES = 1_000_000
DEFAULT_PACKAGE_SCAN = 50


class HeadRangeReader:
    """Bound the sampler to a head read; a non-head range is refused, not fetched."""

    def __init__(self, transport: WebDavArchiveTransport, *, head_bytes: int = HEAD_SAMPLE_BYTES) -> None:
        self._transport = transport
        self._head_bytes = max(1, int(head_bytes))

    async def read_range(self, remote_path: str, start: int, end: int) -> bytes:
        if start != 0 or end < 0 or end >= self._head_bytes:
            return b""
        payload = await self._transport.get_bytes(remote_path, max_bytes=end + 1)
        return payload or b""


class ArchiveTransportPort:
    """Adapt the archive transport and package repository to the runner ports."""

    def __init__(
        self,
        transport: WebDavArchiveTransport,
        repository: SQLiteJobRepository,
        *,
        scan: int = DEFAULT_PACKAGE_SCAN,
        package_id: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._transport = transport
        self._repository = repository
        self._scan = max(1, int(scan))
        self._package_id = package_id
        self._dry_run = dry_run

    def _ref(self, package: ArchivePackage) -> PackageRef:
        return PackageRef(
            package_id=package.id,
            remote_path=package.remote_path,
            manifest=package.manifest or {},
        )

    async def packages(self, limit: int | None) -> Sequence[PackageRef]:
        if self._package_id:
            package = await self._repository.get_archive_package(self._package_id)
            return [self._ref(package)] if package is not None else []
        wanted = self._scan if limit is None else min(self._scan, max(1, int(limit)))
        packages = await self._repository.list_archive_packages_by_states(
            (ArchivePackageState.COMMITTED,), limit=wanted
        )
        return [self._ref(package) for package in packages]

    async def read(self, relpath: str, *, max_bytes: int) -> bytes | None:
        return await self._transport.get_bytes(relpath, max_bytes=max_bytes)

    async def write(self, relpath: str, payload: bytes) -> None:
        parent = str(PurePosixPath(relpath).parent)
        if parent and parent != ".":
            await self._transport.ensure_collection(parent)
        await self._transport.put_bytes(relpath, payload)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_PACKAGE_SCAN,
        help="how many committed packages to inspect in this run",
    )
    result.add_argument("--package-id", default=None, help="backfill exactly one package")
    result.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written without touching the archive",
    )
    result.add_argument(
        "--head-bytes",
        type=int,
        default=HEAD_SAMPLE_BYTES,
        help="bounded head sample used to decode one frame",
    )
    return result


async def run_backfill(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    repository = SQLiteJobRepository(settings.data_dir / "state.sqlite3")
    await repository.open()
    transport = WebDavArchiveTransport(
        settings.archive_url,
        settings.archive_user,
        settings.archive_password,
        capability_root=settings.archive_remote_root,
        response_timeout=float(settings.archive_response_timeout_seconds),
        verify_attempts=settings.archive_verify_attempts,
        verify_interval_seconds=float(settings.archive_verify_interval_seconds),
    )
    try:
        port = ArchiveTransportPort(
            transport,
            repository,
            scan=args.limit,
            package_id=args.package_id,
            dry_run=args.dry_run,
        )
        sampler = HeadSampleCoverPort(
            HeadRangeReader(transport, head_bytes=args.head_bytes),
            FFmpegMediaTransformer(),
            head_bytes=args.head_bytes,
            work_root=Path(tempfile.gettempdir()),
        )
        runner = CoverBackfillRunner(
            port,
            CoverBackfill(sampler),
            limit=args.limit,
            dry_run=args.dry_run,
            index_max_bytes=INDEX_MAX_BYTES,
        )
        result = await runner.run()
        print(json.dumps({"status": "ok", "dry_run": bool(args.dry_run), **result.as_dict()}))
        return 0
    finally:
        closer = getattr(repository, "close", None)
        if closer is not None:
            await closer()


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run_backfill(args))
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
