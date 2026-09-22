from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from dataclasses import replace

from tgvio_player.application.catalog import CatalogSyncService
from tgvio_player.domain.catalog import ArchivePackageCandidate
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_catalog import (
    WebDavArchiveCatalogSource,
    WebDavCollectionEntry,
)
from tgvio_player.infrastructure.migration import PlayerMigrationError
from tgvio_player.testing.fake_archive_source import FakeArchiveCatalogSource


def canonical_sha(payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def candidate(
    package_id: str,
    digest: str,
    *,
    remote_path: str = "TGVIO/2026-09-22/1",
    relpath: str = "video.mp4",
    kind: str = "video",
    size: int = 1234,
    manifest_patch: dict[str, object] | None = None,
    complete_patch: dict[str, object] | None = None,
) -> ArchivePackageCandidate:
    manifest: dict[str, object] = {
        "schema": "tgvio.archive/v2",
        "package_id": package_id,
        "job_id": "job-1",
        "created_at": "2026-09-22T00:00:00Z",
        "archive_profile": {
            "id": "primary",
            "policy": "required",
            "policy_version": 1,
        },
        "media_count": 1,
        "media": [
            {
                "index": 1,
                "kind": kind,
                "path": relpath,
                "original_name": "video.mp4",
                "size_bytes": size,
                "sha256": digest,
                "mime_type": "video/mp4" if kind == "video" else "image/jpeg",
                "width": 1080,
                "height": 1920,
                "duration_seconds": 12.5 if kind == "video" else None,
                "container": "mp4" if kind == "video" else None,
                "codec": "h264" if kind == "video" else None,
            }
        ],
    }
    if manifest_patch:
        manifest.update(manifest_patch)
    complete: dict[str, object] = {
        "schema": "tgvio.archive.complete/v1",
        "package_id": package_id,
        "manifest_sha256": canonical_sha(manifest),
        "media_count": 1,
        "total_bytes": size,
    }
    if complete_patch:
        complete.update(complete_patch)
    return ArchivePackageCandidate(
        remote_path=remote_path,
        manifest=manifest,
        complete=complete,
        manifest_etag='"manifest"',
        complete_etag='"complete"',
    )


class PlayerCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(
            Path(self.tmp.name) / "player.sqlite3"
        )
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_committed_video_enters_catalog(self) -> None:
        digest = "a" * 64
        source = FakeArchiveCatalogSource(
            packages=[candidate("arc_one", digest)]
        )
        result = await CatalogSyncService(source, self.repo).sync_once()
        self.assertEqual(result.committed, 1)
        self.assertEqual(result.rejected, 0)
        self.assertEqual(result.active_videos, 1)
        self.assertEqual(await self.repo.list_active_video_ids(), [digest])

    async def test_complete_marker_must_match_manifest_hash(self) -> None:
        item = candidate("arc_bad", "b" * 64)
        item.complete["manifest_sha256"] = "0" * 64
        result = await CatalogSyncService(
            FakeArchiveCatalogSource(packages=[item]),
            self.repo,
        ).sync_once()
        self.assertEqual(result.committed, 0)
        self.assertEqual(result.rejected, 1)
        self.assertEqual(result.active_videos, 0)

    async def test_non_object_metadata_is_rejected_without_stopping_other_packages(self) -> None:
        good = candidate("arc_good", "b" * 64)
        malformed = candidate("arc_bad", "c" * 64)
        malformed = replace(malformed, manifest=[])
        result = await CatalogSyncService(
            FakeArchiveCatalogSource(packages=[good, malformed]), self.repo
        ).sync_once()
        self.assertEqual(result.committed, 1)
        self.assertEqual(result.rejected, 1)
        self.assertEqual(result.active_videos, 1)

    async def test_path_traversal_is_rejected(self) -> None:
        result = await CatalogSyncService(
            FakeArchiveCatalogSource(
                packages=[
                    candidate(
                        "arc_escape",
                        "c" * 64,
                        relpath="../escape.mp4",
                    )
                ]
            ),
            self.repo,
        ).sync_once()
        self.assertEqual(result.rejected, 1)
        self.assertEqual(result.active_videos, 0)

    async def test_same_sha_across_packages_is_one_media_with_two_locations(self) -> None:
        digest = "d" * 64
        source = FakeArchiveCatalogSource(
            packages=[
                candidate(
                    "arc_a",
                    digest,
                    remote_path="TGVIO/2026-09-22/1",
                ),
                candidate(
                    "arc_b",
                    digest,
                    remote_path="TGVIO/2026-09-22/2",
                ),
            ]
        )
        result = await CatalogSyncService(source, self.repo).sync_once()
        self.assertEqual(result.active_videos, 1)
        self.assertEqual(len(await self.repo.active_locations(digest)), 2)

    async def test_missing_package_deactivates_only_its_location(self) -> None:
        digest = "e" * 64
        source = FakeArchiveCatalogSource(
            packages=[
                candidate(
                    "arc_a",
                    digest,
                    remote_path="TGVIO/2026-09-22/1",
                ),
                candidate(
                    "arc_b",
                    digest,
                    remote_path="TGVIO/2026-09-22/2",
                ),
            ]
        )
        service = CatalogSyncService(source, self.repo)
        await service.sync_once()
        source.packages = [source.packages[1]]
        result = await service.sync_once()
        self.assertEqual(result.inactive_packages, 1)
        self.assertEqual(result.active_videos, 1)
        self.assertEqual(len(await self.repo.active_locations(digest)), 1)

    async def test_media_becomes_inactive_when_all_locations_disappear(self) -> None:
        digest = "f" * 64
        source = FakeArchiveCatalogSource(
            packages=[candidate("arc_a", digest)]
        )
        service = CatalogSyncService(source, self.repo)
        await service.sync_once()
        source.packages = []
        result = await service.sync_once()
        self.assertEqual(result.active_videos, 0)
        self.assertEqual(await self.repo.list_active_video_ids(), [])

    async def test_incomplete_scan_never_deactivates_existing_catalog(self) -> None:
        digest = "1" * 64
        source = FakeArchiveCatalogSource(
            packages=[candidate("arc_a", digest)]
        )
        service = CatalogSyncService(source, self.repo)
        await service.sync_once()
        source.packages = []
        source.complete_scan = False
        result = await service.sync_once()
        self.assertEqual(result.inactive_packages, 0)
        self.assertEqual(result.active_videos, 1)

    async def test_non_video_is_cataloged_but_not_in_v1_feed(self) -> None:
        digest = "2" * 64
        result = await CatalogSyncService(
            FakeArchiveCatalogSource(
                packages=[candidate("arc_photo", digest, kind="photo")]
            ),
            self.repo,
        ).sync_once()
        self.assertEqual(result.committed, 1)
        self.assertEqual(result.active_videos, 0)

    async def test_sync_is_idempotent(self) -> None:
        digest = "3" * 64
        source = FakeArchiveCatalogSource(
            packages=[candidate("arc_same", digest)]
        )
        service = CatalogSyncService(source, self.repo)
        first = await service.sync_once()
        second = await service.sync_once()
        self.assertEqual(first.active_videos, 1)
        self.assertEqual(second.active_videos, 1)
        self.assertEqual(len(await self.repo.active_locations(digest)), 1)

    async def test_bad_package_does_not_erase_good_package(self) -> None:
        good = candidate("arc_good", "4" * 64)
        source = FakeArchiveCatalogSource(packages=[good])
        service = CatalogSyncService(source, self.repo)
        await service.sync_once()
        bad = candidate("arc_bad", "5" * 64)
        bad.complete["total_bytes"] = 999999
        source.packages = [good, bad]
        result = await service.sync_once()
        self.assertEqual(result.committed, 1)
        self.assertEqual(result.rejected, 1)
        self.assertEqual(result.active_videos, 1)

    async def test_present_but_temporarily_invalid_package_keeps_last_good_projection(self) -> None:
        digest = "6" * 64
        original = candidate("arc_flaky", digest)
        source = FakeArchiveCatalogSource(packages=[original])
        service = CatalogSyncService(source, self.repo)
        await service.sync_once()

        broken = candidate("arc_flaky", digest)
        broken.complete["manifest_sha256"] = "0" * 64
        source.packages = [broken]
        result = await service.sync_once()

        self.assertEqual(result.rejected, 1)
        self.assertEqual(result.inactive_packages, 0)
        self.assertEqual(result.active_videos, 1)
        self.assertEqual(await self.repo.list_active_video_ids(), [digest])

    async def test_migration_checksum_change_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            migrations = root / "migrations"
            migrations.mkdir()
            baseline = (
                Path(__file__).parents[1]
                / "src"
                / "tgvio_player"
                / "infrastructure"
                / "migrations"
                / "0001_player_baseline.sql"
            )
            copied = migrations / baseline.name
            shutil.copy2(baseline, copied)
            database = root / "player.sqlite3"
            repository = PlayerCatalogRepositorySQLite(database, migrations_dir=migrations)
            await repository.open()
            await repository.close()
            copied.write_text(copied.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")
            with self.assertRaises(PlayerMigrationError):
                await PlayerCatalogRepositorySQLite(
                    database, migrations_dir=migrations
                ).open()


class FakeWebDavClient:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[WebDavCollectionEntry, ...]] = {}
        self.json: dict[str, object] = {}
        self.reads: list[tuple[str, int]] = []

    async def list_collection(self, remote_path: str) -> tuple[WebDavCollectionEntry, ...]:
        return self.entries[remote_path]

    async def get_json(self, remote_path: str, *, max_bytes: int) -> object | None:
        self.reads.append((remote_path, max_bytes))
        return self.json.get(remote_path)


class WebDavCatalogSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_only_complete_package_metadata(self) -> None:
        source = FakeWebDavClient()
        source.entries["TGVIO"] = (WebDavCollectionEntry("2026-09-22", True),)
        source.entries["TGVIO/2026-09-22"] = (
            WebDavCollectionEntry("1", True),
            WebDavCollectionEntry("2", True),
        )
        source.entries["TGVIO/2026-09-22/1"] = (
            WebDavCollectionEntry("manifest.json", False, '"m"'),
            WebDavCollectionEntry("_COMPLETE.json", False, '"c"'),
            WebDavCollectionEntry("video.mp4", False),
        )
        source.entries["TGVIO/2026-09-22/2"] = (
            WebDavCollectionEntry("manifest.json", False),
        )
        item = candidate("arc_one", "7" * 64)
        source.json["TGVIO/2026-09-22/1/manifest.json"] = item.manifest
        source.json["TGVIO/2026-09-22/1/_COMPLETE.json"] = item.complete

        discovery = await WebDavArchiveCatalogSource(source, remote_root="TGVIO").discover()

        self.assertEqual(len(discovery.packages), 1)
        self.assertFalse(discovery.complete_scan)
        self.assertEqual(discovery.packages[0].manifest_etag, '"m"')
        self.assertTrue(all(limit <= 512 * 1024 for _, limit in source.reads))

    async def test_unsafe_remote_entry_keeps_scan_incomplete(self) -> None:
        source = FakeWebDavClient()
        source.entries["TGVIO"] = (WebDavCollectionEntry("../escape", True),)
        discovery = await WebDavArchiveCatalogSource(source, remote_root="TGVIO").discover()
        self.assertEqual(discovery.packages, ())
        self.assertFalse(discovery.complete_scan)


if __name__ == "__main__":
    unittest.main()
