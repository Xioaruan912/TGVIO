from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.pick_previews import PickPreviewService

KEY = "-100555"


class FakeCandidate:
    def __init__(self, path: Path, needs_frame: bool = False) -> None:
        self.path = path
        self.needs_frame = needs_frame


class FakeFrames:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    async def extract(self, source: Path, output: Path):
        self.calls.append((str(source), str(output)))
        if not self.ok:
            return None
        Path(output).write_bytes(b"frame")
        return Path(output)


def recording_fetch(calls: list[int], *, needs_frame: bool = False):
    async def fetch(source_index: int, message_id: int, target_dir: Path):
        calls.append(int(message_id))
        path = Path(target_dir) / f"thumb-{message_id}.{'mp4' if needs_frame else 'jpg'}"
        path.write_bytes(b"thumb")
        return FakeCandidate(path, needs_frame=needs_frame) if needs_frame else path

    return fetch


class ThumbnailTests(unittest.IsolatedAsyncioTestCase):
    async def test_thumbnail_is_cached_per_source_and_reused(self) -> None:
        with TemporaryDirectory() as tmp:
            calls: list[int] = []
            service = PickPreviewService(recording_fetch(calls), cache_root=Path(tmp), key_provider=lambda index: KEY)
            first = await service.thumbnail(0, 10)
            second = await service.thumbnail(0, 10)
            self.assertEqual(calls, [10], "a cached thumbnail must not download again")
            self.assertEqual(first, second)
            assert first is not None
            self.assertEqual(first.parent, Path(tmp) / "pickthumb" / KEY)
            self.assertEqual(first.read_bytes(), b"thumb")
            # The per-call working directory is gone; only the cache stays.
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["pickthumb"])

    async def test_expired_entries_are_fetched_again(self) -> None:
        with TemporaryDirectory() as tmp:
            calls: list[int] = []
            clock = {"value": 1_000_000.0}
            service = PickPreviewService(recording_fetch(calls), cache_root=Path(tmp),
                                         key_provider=lambda index: KEY, now=lambda: clock["value"])
            await service.thumbnail(0, 10)
            clock["value"] += 49 * 3600
            await service.thumbnail(0, 10)
            self.assertEqual(calls, [10, 10])

    async def test_without_a_cache_key_there_is_no_image_to_serve(self) -> None:
        with TemporaryDirectory() as tmp:
            calls: list[int] = []
            service = PickPreviewService(recording_fetch(calls), cache_root=Path(tmp))
            self.assertIsNone(await service.thumbnail(0, 10))
            self.assertFalse((Path(tmp) / "pickthumb").exists())

    async def test_unsafe_cache_keys_are_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            service = PickPreviewService(recording_fetch([]), cache_root=Path(tmp), key_provider=lambda index: "../x")
            self.assertIsNone(await service.thumbnail(0, 10))

    async def test_a_message_without_preview_yields_none(self) -> None:
        async def fetch(source_index, message_id, target_dir):
            return None

        with TemporaryDirectory() as tmp:
            service = PickPreviewService(fetch, cache_root=Path(tmp), key_provider=lambda index: KEY)
            self.assertIsNone(await service.thumbnail(0, 10))

    async def test_a_hanging_fetch_is_bounded(self) -> None:
        async def fetch(source_index, message_id, target_dir):
            await asyncio.Event().wait()

        with TemporaryDirectory() as tmp:
            service = PickPreviewService(fetch, cache_root=Path(tmp), key_provider=lambda index: KEY, timeout_seconds=5)
            service._timeout = 0.05
            self.assertIsNone(await service.thumbnail(0, 10))


class FrameFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_fragment_is_turned_into_a_frame(self) -> None:
        with TemporaryDirectory() as tmp:
            frames = FakeFrames()
            service = PickPreviewService(recording_fetch([], needs_frame=True), cache_root=Path(tmp),
                                         key_provider=lambda index: KEY, frame_extractor=frames)
            image = await service.thumbnail(0, 8381)
            self.assertEqual(len(frames.calls), 1)
            assert image is not None
            self.assertEqual(image.read_bytes(), b"frame")

    async def test_undecodable_fragment_or_missing_extractor_yields_none(self) -> None:
        with TemporaryDirectory() as tmp:
            for frames in (FakeFrames(ok=False), None):
                service = PickPreviewService(recording_fetch([], needs_frame=True), cache_root=Path(tmp),
                                             key_provider=lambda index: KEY, frame_extractor=frames)
                self.assertIsNone(await service.thumbnail(0, 8381))


if __name__ == "__main__":
    unittest.main()
