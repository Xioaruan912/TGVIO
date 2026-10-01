from __future__ import annotations

import shutil
from pathlib import Path
import tempfile
import unittest

from tgvio.infrastructure.cover_frames import sample, flat_extreme_frame
from tgvio.infrastructure.rendition_encoder import run_process


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
class BrightFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_real_detail_on_bright_background_is_a_usable_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "fixture.mp4"
            await run_process("ffmpeg", "-nostdin", "-y", "-v", "error",
                              "-f", "lavfi", "-i", "color=white:size=640x848:rate=30",
                              "-vf", "drawbox=x=250:y=400:w=40:h=28:color=black:t=fill",
                              "-t", "1", "-c:v", "libx264", "-threads", "1",
                              str(original), timeout=30)
            payload = original.read_bytes()
            class Reader:
                total = 0
                async def read_range(self, path, start, end, size):
                    self.total += end-start+1
                    return payload[start:end+1]
            reader = Reader()
            work = root / "sample";work.mkdir()
            cover = await sample(reader, "fixture/video.mp4", len(payload), work)
            self.assertIsNotNone(cover, "a bright frame containing real detail is not blank")
            self.assertTrue(cover.startswith(b"\xff\xd8") and cover.endswith(b"\xff\xd9"))
            self.assertLessEqual(reader.total, 12*1024**2)


class DetailPolicyTests(unittest.TestCase):
    def test_flat_and_compression_noise_are_still_rejected(self):
        for gray in [bytes(1024), bytes([255])*1024,
                     bytes([250+i%5 for i in range(1024)]),
                     bytes([i%5 for i in range(1024)])]:
            self.assertTrue(flat_extreme_frame(gray))

    def test_small_bright_and_dark_content_is_preserved(self):
        self.assertFalse(flat_extreme_frame(bytes([255])*1011+bytes([80])*13))
        self.assertFalse(flat_extreme_frame(bytes(1011)+bytes([170])*13))
        self.assertFalse(flat_extreme_frame(bytes(range(256))*4))
