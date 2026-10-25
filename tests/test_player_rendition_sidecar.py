from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from tgvio_player.application.catalog import CatalogSyncService
from tgvio_player.application.rendition_sidecar import parse_renditions
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.testing.fake_archive_source import FakeArchiveCatalogSource
from tgvio_player.infrastructure.webdav_catalog import WebDavArchiveCatalogSource, WebDavCollectionEntry
from test_player_catalog import candidate

# renditions.json written by the retired maintenance worker; old packages still carry it.
SCHEMA = "tgvio.archive.renditions/v1"
ALGORITHM = "h264-height-crf24-v1"


def fixture():
    return candidate("package", "a" * 64), None

def document(original):
    return {"schema": SCHEMA, "package_id": "package", "algorithm": ALGORITHM,
            "manifest_sha256": original.complete["manifest_sha256"],
            "media": [{
                "path": f"renditions/{h}.mp4", "sha256": digest * 64,
                "variant_of": "a" * 64, "height": h, "width": w,
                "size_bytes": 1000, "duration_seconds": 12.5,
                "bitrate_bps": 640, "resolution_label": f"{h}p",
                "mime_type": "video/mp4", "codec": "h264",
            } for h, w, digest in [(480, 270, "b"), (720, 406, "c")]]}


class SidecarTests(unittest.IsolatedAsyncioTestCase):
    async def test_projection_keeps_original_and_hides_renditions_from_feed(self):
        original, _ = fixture()
        original = replace(original, renditions=document(original))
        with tempfile.TemporaryDirectory() as tmp:
            repo = PlayerCatalogRepositorySQLite(Path(tmp) / "player.sqlite3")
            await repo.open()
            try:
                result = await CatalogSyncService(
                    FakeArchiveCatalogSource(packages=[original]), repo).sync_once()
                self.assertEqual(result.active_videos, 1)
                self.assertEqual(repo._require().execute("SELECT COUNT(*) FROM media_variants").fetchone()[0], 2)
                self.assertEqual(repo._require().execute("SELECT COUNT(*) FROM media").fetchone()[0], 3)
                self.assertEqual(repo._require().execute(
                    "SELECT size_bytes FROM media WHERE media_id=?", ("a" * 64,)).fetchone()[0], 1234)
                await CatalogSyncService(
                    FakeArchiveCatalogSource(packages=[original]), repo).sync_once()
                self.assertEqual(repo._require().execute("SELECT COUNT(*) FROM media_variants").fetchone()[0], 2)
            finally:
                await repo.close()

    def test_invalid_sidecar_entries_are_optional(self):
        original, _ = fixture()
        package = CatalogSyncService._validate(original)
        valid = document(original)
        self.assertEqual(len(parse_renditions(package, valid)), 2)
        for field, bad in [("height", True), ("path", "../original.mp4"),
                           ("variant_of", "d" * 64), ("sha256", "a" * 64),
                           ("size_bytes", True), ("duration_seconds", float("nan")),
                           ("duration_seconds", 100), ("bitrate_bps", 99_000_000)]:
            mutated = json.loads(json.dumps(valid))
            for item in mutated["media"]:
                item[field] = bad
            self.assertEqual(parse_renditions(package, mutated), (), field)
        self.assertEqual(parse_renditions(package, {**valid, "manifest_sha256": "f" * 64}), ())

    async def test_discovery_reads_only_listed_bounded_sidecar(self):
        original, _ = fixture()
        class Client:
            calls = []
            async def list_collection(self, path):
                if path == "TGVIO":
                    return (WebDavCollectionEntry("2026-09-22", True),)
                if path.endswith("2026-09-22"):
                    return (WebDavCollectionEntry("1", True),)
                return tuple(WebDavCollectionEntry(n, False) for n in
                             ("manifest.json", "_COMPLETE.json", "renditions.json"))
            async def get_json(self, path, *, max_bytes):
                self.calls.append((path, max_bytes))
                if path.endswith("manifest.json"): return original.manifest
                if path.endswith("_COMPLETE.json"): return original.complete
                return document(original)
        client = Client()
        result = await WebDavArchiveCatalogSource(client, remote_root="TGVIO").discover()
        self.assertEqual(len(result.packages[0].renditions["media"]), 2)
        self.assertTrue(all(limit == 512 * 1024 for _, limit in client.calls))


if __name__ == "__main__":
    unittest.main()
