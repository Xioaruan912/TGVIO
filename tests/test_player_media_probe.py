from __future__ import annotations

import unittest

from tgvio_player.application.media_probe import parse_moov, probe_mp4


def box(kind: bytes, body: bytes) -> bytes:
    return (8 + len(body)).to_bytes(4, "big") + kind + body


def moov(seconds: int, width: int, height: int) -> bytes:
    mvhd = box(b"mvhd", bytes(4) + bytes(8) + (1000).to_bytes(4, "big") + (seconds * 1000).to_bytes(4, "big") + bytes(80))
    tkhd = box(b"tkhd", bytes(76) + (width << 16).to_bytes(4, "big") + (height << 16).to_bytes(4, "big"))
    return box(b"moov", mvhd + box(b"trak", tkhd))


class Mp4ProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_reads_duration_and_frame_size(self) -> None:
        self.assertEqual(parse_moov(moov(754, 1920, 1080)[8:]), (754.0, 1920, 1080))

    async def test_moov_at_the_front_or_after_a_large_mdat(self) -> None:
        ftyp = box(b"ftyp", b"isom" + bytes(12))
        for name, data in (
            ("front", ftyp + moov(42, 720, 1280) + box(b"mdat", bytes(400_000))),
            ("end", ftyp + box(b"mdat", bytes(400_000)) + moov(42, 720, 1280)),
        ):
            reads: list[tuple[int, int]] = []

            async def read(start: int, end: int, data: bytes = data) -> bytes:
                reads.append((start, end))
                return data[start:end + 1]

            with self.subTest(layout=name):
                self.assertEqual(await probe_mp4(len(data), read), (42.0, 720, 1280))
                self.assertLessEqual(len(reads), 2)

    async def test_not_an_mp4_gives_nothing(self) -> None:
        async def read(start: int, end: int) -> bytes:
            return b"<html>advert</html>"[start:end + 1]

        self.assertIsNone(await probe_mp4(19, read))


if __name__ == "__main__":
    unittest.main()
