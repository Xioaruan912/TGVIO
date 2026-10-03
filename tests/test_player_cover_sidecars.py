from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_player_catalog import candidate
from tgvio_player.application.catalog import CatalogSyncService
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.testing.fake_archive_source import FakeArchiveCatalogSource


def document(original):
    return {"schema": "tgvio.archive.covers/v2", "package_id": "package",
            "manifest_sha256": original.complete["manifest_sha256"],
            "algorithm": "bounded-frame-v2", "covers": {
                "video.mp4": {"path": "cover/backfill/" + "b" * 64 + ".jpg",
                              "media_sha256": "a" * 64, "sha256": "b" * 64,
                              "size_bytes": 1200, "mime_type": "image/jpeg"}}}


class CoverSidecarTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_cover_is_projected_and_returns_version_without_new_media(self):
        original = candidate("package", "a" * 64)
        value = replace(original, covers=document(original))
        with TemporaryDirectory() as tmp:
            repo = PlayerCatalogRepositorySQLite(Path(tmp) / "player.sqlite3")
            await repo.open()
            try:
                result = await CatalogSyncService(FakeArchiveCatalogSource(packages=[value]), repo).sync_once()
                self.assertEqual(result.active_videos, 1)
                cover = await repo.active_cover("a" * 64)
                self.assertEqual(cover["size_bytes"], 1200)
                self.assertEqual(len(cover["version"]), 64)
                self.assertEqual(repo._require().execute("SELECT COUNT(*) FROM media").fetchone()[0], 1)
                await CatalogSyncService(FakeArchiveCatalogSource(packages=[value]), repo).sync_once()
                self.assertEqual(repo._require().execute("SELECT COUNT(*) FROM media_covers").fetchone()[0], 1)
            finally:
                await repo.close()

    async def test_transient_sidecar_read_keeps_last_verified_cover(self):
        original = candidate("package", "a" * 64)
        with TemporaryDirectory() as tmp:
            repo = PlayerCatalogRepositorySQLite(Path(tmp) / "player.sqlite3")
            await repo.open()
            try:
                value = replace(original, covers=document(original))
                await CatalogSyncService(FakeArchiveCatalogSource(packages=[value]), repo).sync_once()
                await CatalogSyncService(FakeArchiveCatalogSource(
                    packages=[replace(original, cover_read_failed=True)]), repo).sync_once()
                self.assertIsNotNone(await repo.active_cover("a" * 64))
                await CatalogSyncService(FakeArchiveCatalogSource(packages=[original]), repo).sync_once()
                self.assertIsNone(await repo.active_cover("a" * 64))
            finally:
                await repo.close()

    def test_wrong_binding_and_malformed_entries_do_not_hide_original(self):
        original = candidate("package", "a" * 64)
        for field, bad in [("package_id", "other"), ("manifest_sha256", "f" * 64),
                           ("schema", "tgvio.archive.covers/v1"), ("algorithm", "unknown")]:
            package = CatalogSyncService._validate(replace(original, covers={**document(original), field: bad}))
            self.assertEqual(package.covers, (), field)
            self.assertEqual(len(package.media), 1)
        for field, bad in [("path", "../private.jpg"), ("path", "cover/%2e%2e/x.jpg"),
                           ("path", "video.mp4"), ("size_bytes", True),
                           ("size_bytes", 1000001), ("mime_type", "video/mp4"),
                           ("media_sha256", "c" * 64), ("sha256", "bad")]:
            value = document(original)
            value["covers"]["video.mp4"][field] = bad
            self.assertEqual(CatalogSyncService._validate(replace(original, covers=value)).covers, (), field)

    async def test_discovery_reads_only_listed_cover_sidecar_with_metadata_budget(self):
        from tgvio_player.infrastructure.webdav_catalog import WebDavArchiveCatalogSource, WebDavCollectionEntry
        original = candidate("package", "a" * 64)
        class Client:
            calls = []
            async def list_collection(self, path):
                if path == "TGVIO": return (WebDavCollectionEntry("date", True),)
                if path == "TGVIO/date": return (WebDavCollectionEntry("package", True),)
                return tuple(WebDavCollectionEntry(n, False) for n in ("manifest.json", "_COMPLETE.json", "covers.json"))
            async def get_json(self, path, *, max_bytes):
                self.calls.append((path, max_bytes))
                return {"manifest.json": original.manifest, "_COMPLETE.json": original.complete,
                        "covers.json": document(original)}[path.rsplit("/", 1)[-1]]
        client = Client()
        found = await WebDavArchiveCatalogSource(client, remote_root="TGVIO").discover()
        self.assertEqual(found.packages[0].covers, document(original))
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(all(limit == 512 * 1024 for _, limit in client.calls))

class CoverFingerprintTests(unittest.IsolatedAsyncioTestCase):
    """The fingerprint is optional, additive, and never costs the cover it belongs to."""

    def document_with(self, original, phash):
        value = document(original)
        value["covers"]["video.mp4"]["phash"] = phash
        return value

    async def test_a_valid_fingerprint_is_projected_with_the_cover(self):
        original = candidate("package", "a" * 64)
        value = replace(original, covers=self.document_with(original, "0123456789abcdef"))
        with TemporaryDirectory() as tmp:
            repo = PlayerCatalogRepositorySQLite(Path(tmp) / "player.sqlite3")
            await repo.open()
            try:
                await CatalogSyncService(FakeArchiveCatalogSource(packages=[value]), repo).sync_once()
                cover = await repo.active_cover("a" * 64)
                self.assertEqual(cover["phash"], "0123456789abcdef")
            finally:
                await repo.close()

    async def test_a_missing_or_broken_fingerprint_never_costs_the_cover(self):
        for broken in (None, "0123456789ABCDEF", "0123456789abcde", "0123456789abcdeg", "", 17):
            with self.subTest(phash=broken):
                original = candidate("package", "a" * 64)
                value = replace(original, covers=self.document_with(original, broken))
                with TemporaryDirectory() as tmp:
                    repo = PlayerCatalogRepositorySQLite(Path(tmp) / "player.sqlite3")
                    await repo.open()
                    try:
                        await CatalogSyncService(FakeArchiveCatalogSource(packages=[value]), repo).sync_once()
                        cover = await repo.active_cover("a" * 64)
                        self.assertIsNotNone(cover, "the cover survives a fingerprint it cannot read")
                        self.assertEqual(cover["phash"], None)
                        self.assertEqual(
                            repo._require().execute(
                                "SELECT COUNT(*) FROM media_covers"
                            ).fetchone()[0], 1,
                        )
                    finally:
                        await repo.close()
