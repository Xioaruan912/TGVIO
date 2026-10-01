from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from tgvio.application.rendition_backfill import RenditionBackfill, canonical
from tgvio.domain.renditions import ALGORITHM, SCHEMA, RenditionTask, safe_path
from tgvio.infrastructure.rendition_encoder import encode, run_process
from tgvio.infrastructure.rendition_state import RenditionState
from tgvio.adapters.rendition_discovery import RenditionDiscovery
from tgvio_player.application.catalog import CatalogSyncService
from tgvio_player.application.rendition_sidecar import parse_renditions
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.testing.fake_archive_source import FakeArchiveCatalogSource
from tgvio_player.infrastructure.webdav_catalog import WebDavArchiveCatalogSource, WebDavCollectionEntry
from test_player_catalog import candidate


def fixture():
    original = candidate("package", "a" * 64)
    task = RenditionTask("package", original.remote_path,
                         original.complete["manifest_sha256"], original.manifest["media"][0])
    return original, task


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


class FakePort:
    def __init__(self):
        self.original, self.task = fixture()
        self.objects = {
            "manifest.json": self.original.manifest,
            "_COMPLETE.json": self.original.complete,
        }
        self.calls = []
        self.fail_height = None

    async def read_json(self, path):
        return self.objects.get(path.rsplit("/", 1)[-1])

    async def exists(self, path, size):
        if path.endswith("/video.mp4"):
            return True
        return self.objects.get(path) == size

    async def recover(self, task, heights, work, max_bytes):
        return {}

    async def download(self, path, target, size, digest):
        self.calls.append(("download", path))
        target.write_bytes(b"fixture")

    async def encode(self, source, target, height):
        self.calls.append(("encode", height))
        if height == self.fail_height:
            raise RuntimeError("fixture encode failure")
        target.write_bytes(str(height).encode())
        return {"height": height, "width": 270 if height == 480 else 406,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "duration_seconds": 12.5, "size_bytes": 3, "bitrate_bps": 2}

    async def upload(self, source, path):
        self.calls.append(("upload", path))
        self.objects[path] = source.stat().st_size

    async def write_json(self, path, value):
        self.calls.append(("index", path))
        self.objects["renditions.json"] = json.loads(canonical(value))


class BackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_before_index_and_original_bytes_unchanged(self):
        port = FakePort()
        before = canonical(port.original.manifest), canonical(port.original.complete)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(await RenditionBackfill(port).run(port.task, Path(tmp)), 2)
        self.assertEqual(before, (canonical(port.objects["manifest.json"]),
                                  canonical(port.objects["_COMPLETE.json"])))
        self.assertEqual([x[0] for x in port.calls],
                         ["download", "encode", "upload", "index", "encode", "upload", "index"])

    async def test_resume_keeps_finished_480_and_only_encodes_720(self):
        port = FakePort()
        port.fail_height = 720
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                await RenditionBackfill(port).run(port.task, Path(tmp))
            self.assertEqual(len(port.objects["renditions.json"]["media"]), 1)
            port.calls.clear()
            port.fail_height = None
            self.assertEqual(await RenditionBackfill(port).run(port.task, Path(tmp)), 1)
            self.assertEqual([v for k, v in port.calls if k == "encode"], [720])
            port.calls.clear()
            self.assertEqual(await RenditionBackfill(port).run(port.task, Path(tmp)), 0)
            self.assertEqual(port.calls, [])

    async def test_missing_remote_rendition_is_repaired(self):
        port = FakePort()
        with tempfile.TemporaryDirectory() as tmp:
            await RenditionBackfill(port).run(port.task, Path(tmp))
            entry = port.objects["renditions.json"]["media"][0]
            del port.objects[port.task.root + "/" + entry["path"]]
            self.assertEqual(await RenditionBackfill(port).run(port.task, Path(tmp)), 1)

    async def test_changed_manifest_refuses_all_writes(self):
        port = FakePort()
        port.objects["manifest.json"] = {"package_id": "other"}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                await RenditionBackfill(port).run(port.task, Path(tmp))
        self.assertEqual(port.calls, [])

    async def test_conflicting_sidecar_is_not_overwritten(self):
        port = FakePort()
        port.objects["renditions.json"] = {"schema": "unknown"}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                await RenditionBackfill(port).run(port.task, Path(tmp))
        self.assertEqual(port.calls, [])

    async def test_watch_rescans_after_ten_tasks_and_keeps_completed_checkpoints(self):
        from tgvio.interfaces.backfill_renditions import work
        original, task = fixture()
        tasks = [replace(task, package_id=f"package-{i}") for i in range(30)]
        discovery = SimpleNamespace(tasks=AsyncMock(return_value=tasks))
        runner = SimpleNamespace(run=AsyncMock(return_value=0))
        scans = 0
        async def sleep(seconds):
            nonlocal scans
            if seconds == 60:
                scans += 1
                if scans == 2:
                    raise asyncio.CancelledError()
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(work_dir=tmp, status=False, retry_failed=False,
                                   dry_run=False, watch=True, limit=0)
            with patch.dict("os.environ", {
                "TGVIO_ARCHIVE_WEBDAV_URL": "https://fixture.invalid",
                "TGVIO_ARCHIVE_WEBDAV_USER": "fixture",
                "TGVIO_ARCHIVE_WEBDAV_PASSWORD": "fixture",
            }), patch("tgvio.interfaces.backfill_renditions.RenditionArchivePort"), \
                 patch("tgvio.interfaces.backfill_renditions.RenditionDiscovery", return_value=discovery), \
                 patch("tgvio.interfaces.backfill_renditions.RenditionBackfill", return_value=runner), \
                 patch("tgvio.interfaces.backfill_renditions.report"), \
                 patch("tgvio.interfaces.backfill_renditions.shutil.disk_usage",
                       return_value=SimpleNamespace(free=10 * 1024**3)), \
                 patch("tgvio.interfaces.backfill_renditions.asyncio.sleep", side_effect=sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await work(args)
            self.assertEqual(discovery.tasks.await_count, 2)
            self.assertEqual(runner.run.await_count, 20)
            called = [call.args[0].key for call in runner.run.await_args_list]
            self.assertEqual(len(set(called)), 20)

    def test_no_upscale_and_traversal(self):
        _, task = fixture()
        self.assertEqual(replace(task, media={**task.media, "height": 480}).required, ())
        self.assertEqual(replace(task, media={**task.media, "height": 720}).required, (480,))
        for path in ["../x", "/root/x", "a/%2e%2e/x", "a//x", "a\\x"]:
            with self.assertRaises(ValueError):
                safe_path(path)


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


class StateTests(unittest.TestCase):
    def test_checkpoint_retry_budget_and_read_only_bot_source(self):
        _, task = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = RenditionState(root / "work.sqlite3")
            state.discover([task])
            self.assertTrue(state.eligible(task))
            for _ in range(5):
                state.fail(task, ValueError())
            self.assertFalse(state.eligible(task, now=10**12))
            self.assertEqual(state.summary()["blocked"], 1)
            state.finish(task, 2)
            self.assertFalse(state.eligible(task))
            state.conn.close()
            state = RenditionState(root / "work.sqlite3")
            self.assertEqual(state.summary()["done"], 1)
            state.conn.close()

    def test_discovery_includes_packages_outside_bot_database(self):
        original, _ = fixture()
        class Discovery(RenditionDiscovery):
            async def collections(self, path):
                return ("2026-09-22",) if path == "TGVIO" else ("1",)
        class Port:
            async def read_json(self, path):
                return original.complete if path.endswith("_COMPLETE.json") else original.manifest
        tasks = asyncio.run(Discovery(Port(), "TGVIO").tasks())
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].media["sha256"], "a" * 64)



@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
class ActualEncoderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_progressive_renditions_dimensions_duration_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fixture.mp4"
            await run_process("ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                              "testsrc2=size=1280x960:rate=30", "-t", "1", "-c:v", "libx264",
                              "-threads", "2", str(source), timeout=60)
            for height in (480, 720):
                target = root / f"{height}.mp4"
                spec = await encode(source, target, height)
                self.assertEqual(spec["height"], height)
                self.assertAlmostEqual(spec["duration_seconds"], 1, delta=.1)
                self.assertEqual(spec["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
                data = target.read_bytes()
                self.assertLess(data.index(b"moov"), data.index(b"mdat"))

    async def test_distinct_source_hashes_cannot_alias_one_variant_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fixture.mp4"
            other = root / "other.mp4"
            await run_process("ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                              "testsrc2=size=640x960:rate=30", "-t", "0.5",
                              "-c:v", "libx264", "-threads", "2", str(source), timeout=60)
            await run_process("ffmpeg", "-y", "-v", "error", "-i", str(source),
                              "-c", "copy", "-metadata", "comment=fixture-other-source",
                              str(other), timeout=60)
            a = await encode(source, root / "a.mp4", 480)
            b = await encode(other, root / "b.mp4", 480)
            self.assertNotEqual(a["sha256"], b["sha256"])
            self.assertEqual(a["width"], b["width"])
            self.assertEqual(a["duration_seconds"], b["duration_seconds"])

    async def test_cancelled_process_is_reaped(self):
        with self.assertRaises(asyncio.TimeoutError):
            await run_process("python3", "-c", "import time;time.sleep(10)", timeout=.05)
