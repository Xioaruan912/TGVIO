from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_player_catalog import candidate
from tgvio.application.cover_backfill_runner import CommittedCoverBackfill
from tgvio.domain.renditions import RenditionTask
from tgvio.infrastructure.cover_frames import sample
from tgvio.adapters.cover_archive import CoverArchivePort
from tgvio.infrastructure.rendition_state import MaintenanceState


class Port:
    def __init__(self):
        self.original = candidate("package", "a" * 64)
        self.task = RenditionTask("package", self.original.remote_path,
                                  self.original.complete["manifest_sha256"],
                                  self.original.manifest["media"][0])
        self.index = None
        self.objects = {}
        self.calls = []
        self.failure = False
        self.image = b"\xff\xd8fixture\xff\xd9"

    async def read_json(self, path):
        return {"manifest.json": self.original.manifest, "_COMPLETE.json": self.original.complete,
                "covers.json": self.index}.get(path.rsplit("/", 1)[-1])
    async def exists(self, path, size):
        return path.endswith("/video.mp4") or self.objects.get(path) == size
    async def sample(self, path, size, work):
        self.calls.append("sample")
        return self.image
    async def write_cover(self, path, payload):
        self.calls.append("image")
        if self.failure: raise ValueError("fixture verification failure")
        self.objects[path] = len(payload)
    async def write_json(self, path, value):
        self.calls.append("index")
        self.index = value


class CommittedCoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_image_then_bound_index_and_resume_without_decode(self):
        p = Port()
        before = p.original.manifest.copy(), p.original.complete.copy()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(await CommittedCoverBackfill(p).run(p.task, Path(tmp)), 1)
            self.assertEqual(p.calls, ["sample", "image", "index"])
            self.assertEqual(p.index["manifest_sha256"], p.task.manifest_hash)
            self.assertEqual(p.index["covers"]["video.mp4"]["media_sha256"], "a" * 64)
            p.calls.clear()
            self.assertEqual(await CommittedCoverBackfill(p).run(p.task, Path(tmp)), 0)
            self.assertEqual(p.calls, [])
        self.assertEqual(before, (p.original.manifest, p.original.complete))

    async def test_failed_upload_does_not_commit_index(self):
        p = Port(); p.failure = True
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                await CommittedCoverBackfill(p).run(p.task, Path(tmp))
        self.assertIsNone(p.index)

    async def test_missing_object_repaired_and_other_entries_preserved(self):
        p = Port()
        with tempfile.TemporaryDirectory() as tmp:
            await CommittedCoverBackfill(p).run(p.task, Path(tmp))
            p.index["covers"]["other.mp4"] = {"fixture": "preserve"}
            p.objects.clear()
            await CommittedCoverBackfill(p).run(p.task, Path(tmp))
        self.assertEqual(p.index["covers"]["other.mp4"], {"fixture": "preserve"})

    async def test_changed_manifest_or_conflicting_index_refuses_all_writes(self):
        for corrupted in ("manifest", "index"):
            p = Port()
            if corrupted == "manifest":
                p.original = replace(p.original, complete={"manifest_sha256": "f" * 64})
            else:
                p.index = {"schema": "unknown"}
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    await CommittedCoverBackfill(p).run(p.task, Path(tmp))
            self.assertEqual(p.calls, [])

    async def test_source_removed_during_sample_does_not_publish(self):
        p = Port()
        async def exists(path, size):
            return not p.calls
        p.exists = exists
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                await CommittedCoverBackfill(p).run(p.task, Path(tmp))
        self.assertEqual(p.calls, ["sample"])

    def test_checkpoint_metrics_and_priority_inputs_are_bounded(self):
        from tgvio.interfaces.backfill_covers import priority_order
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = MaintenanceState(root/"progress.sqlite3", written_label="covers_written")
            self.assertIn("covers_written", state.summary()); state.conn.close()
            priorities = root/"priorities.json"; priorities.write_text('["'+"a"*64+'"]')
            self.assertEqual(priority_order(str(priorities)), {"a"*64: 0})
            priorities.write_text('["../private"]')
            with self.assertRaises(ValueError): priority_order(str(priorities))


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
class CoverFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tail_moov_video_decodes_without_full_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/"original.mp4"; work=root/"work";work.mkdir()
            subprocess.run(["ffmpeg","-nostdin","-v","error","-f","lavfi","-i",
                "testsrc2=s=640x360:r=30","-t","30","-c:v","libx264","-threads","1",
                "-preset","ultrafast","-qp","0",str(source)],check=True,stdin=subprocess.DEVNULL)
            size=source.stat().st_size
            class Reader:
                used=0
                async def read_range(self,path,start,end,total):
                    self.used+=end-start+1
                    with source.open("rb") as f:
                        f.seek(start);return f.read(end-start+1)
            reader=Reader()
            image=await sample(reader,"fixture.mp4",size,work)
            self.assertIsNotNone(image);self.assertTrue(image.startswith(b"\xff\xd8"))
            self.assertLess(reader.used,size)
            self.assertLessEqual(reader.used,12*1024**2)
            self.assertLessEqual(len(image),1_000_000)


class CoverRangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_range_header_and_length_checked(self):
        class Response:
            status=206
            def getheader(self,key):
                return {"Content-Range":"bytes 2-5/100","Content-Length":"4"}.get(key)
            def read(self,size):return b"data"
        class Connection:
            calls=[]
            closed=False
            def request(self,*args,**kw):self.calls.append(kw)
            def getresponse(self):return response
            def close(self):self.closed=True
        response=Response();conn=Connection()
        port=CoverArchivePort("https://fixture.invalid","fixture","fixture")
        with patch.object(port,"_connect",return_value=conn):
            self.assertEqual(await port.read_range("TGVIO/clip.mp4",2,5,100),b"data")
        self.assertEqual(conn.calls[0]["headers"]["Range"],"bytes=2-5")
        self.assertTrue(conn.closed)
        response.status=200
        with patch.object(port,"_connect",return_value=conn):
            with self.assertRaises(ValueError):
                await port.read_range("TGVIO/clip.mp4",2,5,100)
