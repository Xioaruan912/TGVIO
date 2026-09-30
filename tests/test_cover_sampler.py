from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import unittest

from tgvio.infrastructure.cover_sampler import HeadSampleCoverPort


class _Reader:
    def __init__(self, payload: bytes | Exception) -> None:
        self._payload = payload
        self.calls: list[tuple[str, int, int]] = []

    async def read_range(self, remote_path: str, start: int, end: int) -> bytes:
        self.calls.append((remote_path, start, end))
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Transformer:
    def __init__(self, frame: bytes | None, *, delay: float = 0.0, symlink: bool = False) -> None:
        self._frame = frame
        self._delay = delay
        self._symlink = symlink
        self.sampled: list[tuple[int, float | None, int]] = []

    async def make_video_thumbnail(self, source: Path, target_dir: Path, *, item_index: int, duration_seconds: float | None) -> Path | None:
        self.sampled.append((item_index, duration_seconds, source.stat().st_size))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._frame is None:
            return None
        target = target_dir / "thumb-0.jpg"
        if self._symlink:
            real = target_dir / "real.jpg"
            real.write_bytes(self._frame)
            target.symlink_to(real)
            return target
        target.write_bytes(self._frame)
        return target


class HeadSampleCoverPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_bounded_head_sample_decodes_one_frame_from_start(self) -> None:
        reader, transformer = _Reader(b"video-bytes"), _Transformer(b"jpeg")
        with TemporaryDirectory() as tmp:
            port = HeadSampleCoverPort(reader, transformer, work_root=Path(tmp))
            payload = await port("TGVIO/2026/09/22/pkg/clip.mp4")
        self.assertEqual(payload, b"jpeg")
        self.assertEqual(reader.calls, [("TGVIO/2026/09/22/pkg/clip.mp4", 0, 8 * 1024 * 1024 - 1)])
        self.assertEqual(transformer.sampled[0][1], None, "a truncated sample must decode from the start")
        self.assertGreater(transformer.sampled[0][2], 0, "the transformer received the sampled bytes")

    async def test_only_the_head_range_is_requested(self) -> None:
        reader, transformer = _Reader(b"x" * 32), _Transformer(b"jpeg")
        port = HeadSampleCoverPort(reader, transformer, head_bytes=32)
        self.assertEqual(await port("pkg/clip.mp4"), b"jpeg")
        self.assertEqual(reader.calls[0][2], 31)

    async def test_every_failure_mode_yields_no_cover_without_raising(self) -> None:
        cases = [
            (_Reader(b""), _Transformer(b"jpeg"), "empty sample"),
            (_Reader(b"y" * 4096), _Transformer(b"jpeg"), "oversized sample"),
            (_Reader(RuntimeError("webdav down")), _Transformer(b"jpeg"), "reader raises"),
            (_Reader(b"video"), _Transformer(None), "no frame"),
            (_Reader(b"video"), _Transformer(b""), "empty frame"),
        ]
        for reader, transformer, label in cases:
            with self.subTest(label):
                self.assertIsNone(await HeadSampleCoverPort(
                    reader, transformer, head_bytes=1024
                )("pkg/clip.mp4"))

    async def test_a_symlinked_frame_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            port = HeadSampleCoverPort(
                _Reader(b"video"), _Transformer(b"jpeg", symlink=True), work_root=Path(tmp)
            )
            self.assertIsNone(await port("pkg/clip.mp4"))

    async def test_unsafe_paths_never_reach_the_reader(self) -> None:
        reader, transformer = _Reader(b"video"), _Transformer(b"jpeg")
        port = HeadSampleCoverPort(reader, transformer)
        for unsafe in ("/etc/passwd", "../../secret.mp4", "pkg\\clip.mp4", "", "   "):
            self.assertIsNone(await port(unsafe), unsafe)
        self.assertEqual(reader.calls, [])

    async def test_a_slow_sample_is_bounded_by_the_item_timeout(self) -> None:
        reader, transformer = _Reader(b"video"), _Transformer(b"jpeg", delay=1.0)
        port = HeadSampleCoverPort(reader, transformer, item_timeout_seconds=1.0)
        self.assertIsNone(await port("pkg/clip.mp4"))

    async def test_the_temporary_sample_directory_is_removed(self) -> None:
        root = TemporaryDirectory()
        try:
            port = HeadSampleCoverPort(_Reader(b"video"), _Transformer(b"jpeg"), work_root=Path(root.name))
            self.assertEqual(await port("pkg/clip.mp4"), b"jpeg")
            self.assertEqual(list(Path(root.name).iterdir()), [], "nothing is left behind")
        finally:
            root.cleanup()


if __name__ == "__main__":
    unittest.main()
