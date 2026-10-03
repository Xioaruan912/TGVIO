from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from tgvio.infrastructure.cover_frames import sample
from tgvio.infrastructure.rendition_encoder import run_process


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
class LowContrastCoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_low_contrast_detail_is_not_discarded_as_blank(self):
        # Genuine encoded shapes can have useful variation entirely within the
        # dark/bright bands. Preserve their original levels, without brightening.
        for background, detail in [("black", "0x141414"), ("white", "0xebebeb")]:
            with self.subTest(background=background), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root/"fixture.mp4"
                await run_process(
                    "ffmpeg", "-nostdin", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color={background}:size=402x720:rate=15",
                    "-vf", f"drawbox=x=100:y=180:w=200:h=360:color={detail}:t=fill",
                    "-t", "1", "-c:v", "libx264", "-threads", "1",
                    str(source), timeout=30)
                payload = source.read_bytes()
                class Reader:
                    total = 0
                    async def read_range(self, path, start, end, size):
                        self.total += end-start+1
                        return payload[start:end+1]
                reader = Reader()
                work = root/"work";work.mkdir()
                cover = await sample(reader, "fixture/video.mp4", len(payload), work)
                self.assertIsNotNone(cover, "low contrast does not mean no content")
                self.assertTrue(cover.payload.startswith(b"\xff\xd8") and cover.payload.endswith(b"\xff\xd9"))
                self.assertLessEqual(reader.total, 12*1024**2)
                self.assertLessEqual(len(cover.payload), 1_000_000)
