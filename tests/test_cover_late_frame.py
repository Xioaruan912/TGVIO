from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from tgvio.infrastructure.cover_frames import sample
from tgvio.infrastructure.rendition_encoder import run_process


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
class LateCoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_black_intro_can_reach_a_real_frame_without_more_range_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);source = root/"fixture.mp4"
            await run_process(
                "ffmpeg", "-nostdin", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=black:size=320x180:rate=15:duration=8",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=8",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
                "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-qp", "0",
                str(source), timeout=30)
            payload = source.read_bytes()
            class Reader:
                total = 0
                async def read_range(self, path, start, end, size):
                    self.total += end-start+1
                    return payload[start:end+1]
            reader = Reader();work = root/"work";work.mkdir()
            cover = await sample(reader, "fixture/video.mp4", len(payload), work)
            self.assertIsNotNone(cover, "a blank intro must not hide later real content")
            self.assertTrue(cover.startswith(b"\xff\xd8") and cover.endswith(b"\xff\xd9"))
            self.assertLessEqual(reader.total, 12*1024**2)
            self.assertLessEqual(len(cover), 1_000_000)
