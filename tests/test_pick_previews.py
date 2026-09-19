from __future__ import annotations

from pathlib import Path
import time
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.pick_previews import PickPreviewService


class FakeGrid:
    def __init__(self, *, image: bool = True) -> None:
        self.image = image
        self.calls: list[tuple[int, tuple]] = []

    async def build(self, slots, output: Path):
        self.calls.append((len(slots), tuple(slot is not None for slot in slots)))
        if not self.image:
            return None
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"grid-bytes")
        return output

    def grid_shape(self, count: int):
        return (min(5, max(1, count)), 1)


def _fetcher(available: set[int]):
    async def fetch(source_index: int, message_id: int, target_dir: Path):
        if message_id not in available:
            return None
        path = Path(target_dir) / f"thumb-{message_id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        return path

    return fetch


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


def _candidate_fetcher(tmp: Path, *, needs_frame: bool):
    async def fetch(source_index: int, message_id: int, target_dir: Path):
        path = Path(target_dir) / f"clip-{message_id}.mp4"
        path.write_bytes(b"v" * 10)
        return FakeCandidate(path, needs_frame=needs_frame)

    return fetch


class PickPreviewServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, root: Path, fetch, grid=None, **kwargs) -> PickPreviewService:
        return PickPreviewService(fetch, grid or FakeGrid(), cache_root=root, **kwargs)

    async def test_page_preview_tiles_only_the_available_thumbnails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            grid = FakeGrid()
            service = self._service(root, _fetcher({10, 12}), grid)
            progress: list[tuple[int, int]] = []

            async def report(done: int, total: int) -> None:
                progress.append((done, total))

            preview = await service.build_page(0, [10, 11, 12], progress=report)
            self.assertEqual(preview.fetched, 2)
            self.assertEqual(preview.total, 3)
            self.assertEqual(preview.slots, (True, False, True))
            self.assertIsNotNone(preview.image)
            self.assertEqual(grid.calls, [(3, (True, False, True))])
            self.assertEqual(progress[-1], (2, 3))
            self.assertTrue(any(done == 1 for done, _ in progress))
            service.release(preview.token)
            self.assertFalse((root / f"pickpreview-{preview.token}").exists())

    async def test_no_thumbnails_releases_the_cache_and_reports_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._service(root, _fetcher(set()))
            preview = await service.build_page(0, [10, 11])
            self.assertIsNone(preview.image)
            self.assertEqual(preview.fetched, 0)
            self.assertEqual(preview.slots, (False, False))
            self.assertEqual(list(root.iterdir()), [])

    async def test_grid_failure_keeps_the_fetch_result_for_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            service = self._service(Path(tmp), _fetcher({10}), FakeGrid(image=False))
            preview = await service.build_single(0, 10)
            self.assertIsNone(preview.image)
            self.assertEqual(preview.fetched, 1)
            self.assertEqual(preview.slots, (True,))

    async def test_deadline_guard_stops_fetching(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = {"value": 1000.0}

            def now() -> float:
                clock["value"] += 1000.0
                return clock["value"]

            service = self._service(root, _fetcher({10}), now=now)
            preview = await service.build_page(0, [10])
            self.assertEqual(preview.fetched, 0)
            self.assertIsNone(preview.image)

    async def test_single_preview_downloads_one_thumbnail(self) -> None:
        with TemporaryDirectory() as tmp:
            grid = FakeGrid()
            service = self._service(Path(tmp), _fetcher({30506}), grid)
            preview = await service.build_single(1, 30506)
            self.assertEqual(grid.calls, [(1, (True,))])
            self.assertEqual(preview.fetched, 1)
            self.assertIsNotNone(preview.image)
            self.assertEqual(preview.total, 1)

    async def test_sweep_removes_only_stale_preview_dirs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / "pickpreview-old"
            fresh = root / "pickpreview-new"
            other = root / "job-1"
            for directory in (stale, fresh, other):
                directory.mkdir()
            old = time.time() - 7200
            import os

            os.utime(stale, (old, old))
            service = self._service(root, _fetcher(set()))
            self.assertEqual(service.sweep(), 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(other.exists())

    async def test_unsafe_token_is_never_resolved(self) -> None:
        with TemporaryDirectory() as tmp:
            service = self._service(Path(tmp), _fetcher(set()))
            self.assertIsNone(service._safe_dir("../escape"))
            service.release("../escape")


class FrameFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_fragment_is_turned_into_a_frame(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = FakeFrames()
            grid = FakeGrid()
            service = PickPreviewService(
                _candidate_fetcher(root, needs_frame=True),
                grid,
                cache_root=root,
                frame_extractor=frames,
            )
            preview = await service.build_single(0, 8381)
            self.assertEqual(len(frames.calls), 1)
            self.assertIsNotNone(preview.image)
            self.assertEqual(preview.fetched, 1)
            self.assertEqual(grid.calls, [(1, (True,))])

    async def test_undecodable_fragment_yields_no_image(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = PickPreviewService(
                _candidate_fetcher(root, needs_frame=True),
                FakeGrid(),
                cache_root=root,
                frame_extractor=FakeFrames(ok=False),
            )
            with self.assertLogs("tgvio.telegram.preview", level="INFO") as captured:
                preview = await service.build_single(0, 8381)
            self.assertIsNone(preview.image)
            self.assertEqual(preview.fetched, 0)
            self.assertTrue(
                any("No preview image could be resolved" in line for line in captured.output),
                captured.output,
            )

    async def test_missing_frame_extractor_is_tolerated(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = PickPreviewService(
                _candidate_fetcher(root, needs_frame=True),
                FakeGrid(),
                cache_root=root,
            )
            preview = await service.build_single(0, 8381)
            self.assertIsNone(preview.image)


if __name__ == "__main__":
    unittest.main()
