from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from test_rendition_sidecars import FakePort
from tgvio.application.rendition_backfill import RenditionBackfill


class RecoveryPort(FakePort):
    def __init__(self):
        super().__init__()
        self.recovered = {}
        self.removed = False
        self.change_manifest = False

    async def recover(self, task, heights, work, max_bytes):
        self.calls.append(("recover", (tuple(heights), max_bytes)))
        if self.change_manifest:
            self.objects["manifest.json"] = {"package_id": "changed"}
        for spec in self.recovered.values():
            self.objects[task.root + "/" + spec["path"]] = spec["size_bytes"]
        return self.recovered

    async def exists(self, path, size):
        if self.removed and path.endswith("/video.mp4"):
            return False
        return await super().exists(path, size)


def recovered(height=480):
    digest = hashlib.sha256(b"verified-fixture").hexdigest()
    return {"path": f"renditions/{'a'*16}-{height}p-{digest[:16]}.mp4",
            "height": height, "width": 270 if height == 480 else 406,
            "sha256": digest, "size_bytes": 16, "duration_seconds": 12.5,
            "bitrate_bps": 10}


class RecoveryUseCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_remote_outputs_publish_without_download_encode_or_upload(self):
        port = RecoveryPort()
        port.recovered = {480: recovered(), 720: recovered(720)}
        with tempfile.TemporaryDirectory() as tmp:
            runner = RenditionBackfill(port)
            self.assertEqual(await runner.run(port.task, Path(tmp)), 2)
            self.assertEqual(runner.last_recovered, 2)
        self.assertEqual([k for k, _ in port.calls], ["recover", "index", "index"])
        entries = port.objects["renditions.json"]["media"]
        self.assertEqual([e["height"] for e in entries], [480, 720])
        self.assertTrue(all(e["variant_of"] == "a"*64 for e in entries))

    async def test_recovered_480_is_committed_before_720_encode_failure(self):
        port = RecoveryPort()
        port.recovered = {480: recovered()}
        port.fail_height = 720
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                await RenditionBackfill(port).run(port.task, Path(tmp))
        self.assertEqual([e["height"] for e in port.objects["renditions.json"]["media"]], [480])
        self.assertEqual([v for k, v in port.calls if k == "encode"], [720])

    async def test_removed_source_and_changed_manifest_never_publish_recovery(self):
        for changed in (False, True):
            port = RecoveryPort()
            port.recovered = {480: recovered()}
            if changed:
                port.change_manifest = True
            else:
                async def remove(task, heights, work, max_bytes):
                    port.removed = True
                    return port.recovered
                port.recover = remove
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    await RenditionBackfill(port).run(port.task, Path(tmp))
            self.assertNotIn("renditions.json", port.objects)

    async def test_recovery_transfer_reserves_original_budget(self):
        port = RecoveryPort()
        port.task = replace(port.task, media={**port.task.media, "size_bytes": 4*1024**3})
        port.objects["manifest.json"]["media"][0]["size_bytes"] = 4*1024**3
        # Rebind the fixture after changing its canonical manifest.
        from tgvio.domain.renditions import canonical
        digest = hashlib.sha256(canonical(port.objects["manifest.json"])).hexdigest()
        port.objects["_COMPLETE.json"]["manifest_sha256"] = digest
        port.task = replace(port.task, manifest_hash=digest)
        with tempfile.TemporaryDirectory() as tmp:
            await RenditionBackfill(port).run(port.task, Path(tmp))
        self.assertEqual(next(v for k, v in port.calls if k == "recover")[1], 0)

import asyncio
import io
import shutil
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tgvio.adapters.rendition_archive import RenditionArchivePort
from tgvio.adapters.rendition_discovery import RenditionDiscovery
from tgvio.adapters.rendition_recovery import recover
from tgvio.infrastructure.rendition_encoder import encode, file_hash, run_process, verify_recovered


class CandidateTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_hash_size_rate_and_original_full_hash_contract(self):
        port = RenditionArchivePort("http://fixture.invalid", "fixture", "fixture")
        content = b"fixture-media"
        class Response(io.BytesIO):
            status = 200
            def getheader(self, key):
                return str(len(content))
        class Connection:
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return Response(content)
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as tmp, patch.object(port, "_connect", Connection):
            path = Path(tmp) / "candidate.mp4"
            digest = await port.read_candidate("pkg/renditions/a.mp4", path, len(content), time.monotonic()+5)
            self.assertEqual(digest, hashlib.sha256(content).hexdigest())
            self.assertEqual(path.read_bytes(), content)
            with self.assertRaises(ValueError):
                await port.download("pkg/original.mp4", path, len(content), digest[:16])
            with self.assertRaises(ValueError):
                await port.read_candidate("pkg/renditions/a.mp4", path, 64*1024**2+1, time.monotonic()+5)
            with self.assertRaises(ValueError):
                await port.read_candidate("pkg/renditions/a.mp4", path, len(content)-1, time.monotonic()+5)
            with self.assertRaises(TimeoutError):
                await port.read_candidate("pkg/renditions/a.mp4", path, len(content), time.monotonic()-1)

    async def test_cancelled_transfer_is_joined_before_recovery_target_cleanup(self):
        port = RenditionArchivePort("http://fixture.invalid", "fixture", "fixture")
        started, finished = threading.Event(), threading.Event()
        def read(path, target, size, deadline, stop):
            started.set()
            stop.wait(2)
            finished.set()
            return "a"*64
        with patch.object(port, "_read_file", read):
            task = asyncio.create_task(port.read_candidate("pkg/renditions/a.mp4", Path("fixture"), 1, 10))
            await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())

    async def test_listing_rejects_failed_properties_outside_path_and_nested_objects(self):
        rows = []
        for name, status in [("a.mp4", "200 OK"), ("bad.mp4", "404 Not Found"),
                             ("nested/b.mp4", "200 OK"), ("../escaped.mp4", "200 OK")]:
            rows.append(f"<d:response><d:href>/pkg/renditions/{name}</d:href><d:propstat>"
                        f"<d:prop><d:getcontentlength>16</d:getcontentlength></d:prop>"
                        f"<d:status>HTTP/1.1 {status}</d:status></d:propstat></d:response>")
        payload = ('<d:multistatus xmlns:d="DAV:">' + "".join(rows) + '</d:multistatus>').encode()
        class Connection:
            def request(self, *args, **kwargs):
                self.headers = kwargs["headers"]
            def getresponse(self):
                result = io.BytesIO(payload)
                result.status = 207
                return result
            def close(self):
                pass
        port = RenditionArchivePort("http://fixture.invalid", "fixture", "fixture")
        with patch.object(port, "_connect", Connection):
            entries = await RenditionDiscovery(port, "pkg").files("pkg/renditions")
        self.assertEqual(entries, (("a.mp4", 16),))


class CandidateRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_hash_mismatch_is_skipped_without_probe_or_index(self):
        port = RecoveryPort()
        name = f"{'a'*16}-480p-{'b'*16}.mp4"
        port.read_candidate = AsyncMock(return_value="c"*64)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(RenditionDiscovery, "files", new=AsyncMock(return_value=((name, 32),))), \
             patch("tgvio.adapters.rendition_recovery.verify_recovered", new=AsyncMock()) as verify:
            self.assertEqual(await recover(port, port.task, (480,), Path(tmp), 32), {})
            verify.assert_not_awaited()
        port.read_candidate.assert_awaited_once()

    async def test_candidate_and_total_transfer_limits_never_request_oversized_objects(self):
        port = RecoveryPort()
        name = f"{'a'*16}-480p-{'b'*16}.mp4"
        port.read_candidate = AsyncMock(return_value="c"*64)
        for budget, size in [(0, 16), (15, 16), (128*1024**2, 64*1024**2+1)]:
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(RenditionDiscovery, "files", new=AsyncMock(return_value=((name, size),))):
                self.assertEqual(await recover(port, port.task, (480,), Path(tmp), budget), {})
        port.read_candidate.assert_not_awaited()

    async def test_corrupt_candidates_have_bounded_attempts_and_leave_no_local_files(self):
        port = RecoveryPort()
        files = tuple((f"{'a'*16}-480p-{i:016x}.mp4", 16) for i in range(5))
        async def read(path, target, size, deadline):
            target.write_bytes(b"fixture")
            suffix = path.rsplit("-", 1)[1][:-4]
            return suffix + "f"*48
        port.read_candidate = AsyncMock(side_effect=read)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(RenditionDiscovery, "files", new=AsyncMock(return_value=files)), \
             patch("tgvio.adapters.rendition_recovery.verify_recovered",
                   new=AsyncMock(side_effect=ValueError("invalid"))):
            self.assertEqual(await recover(port, port.task, (480,), Path(tmp), 128), {})
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(port.read_candidate.await_count, 2)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
class ActualRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_candidate_is_bound_to_full_source_hash_height_duration_and_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "original.mp4", root / "encoded.mp4"
            await run_process("ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                              "testsrc2=size=640x960:rate=30", "-t", "0.5",
                              "-c:v", "libx264", "-threads", "2", str(source), timeout=30)
            spec = await encode(source, output, 480)
            parent = file_hash(source)
            verified = await verify_recovered(output, parent, 480, .5, spec["sha256"])
            self.assertEqual(verified, spec)
            for changed_parent, height, duration in [("f"*64, 480, .5),
                                                     (parent, 720, .5), (parent, 480, 20)]:
                with self.assertRaises(ValueError):
                    await verify_recovered(output, changed_parent, height, duration, spec["sha256"])
            # A valid container with damaged video packets cannot be indexed.
            data = output.read_bytes()
            mdat = data.index(b"mdat")
            output.write_bytes(data[:mdat+8] + bytes(len(data)-mdat-8))
            with self.assertRaises((ValueError, RuntimeError)):
                await verify_recovered(output, parent, 480, .5, spec["sha256"])
