import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.downloader import CancelToken, DownloadProgress, UrlDownloader


class _FakeYDL:
    behavior = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=True):
        return type(self).behavior(self, url, download)

    def prepare_filename(self, info):
        return str(info.get("prepared") or "")


class UrlDownloaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        url_patch = patch(
            "src.downloader.enforce_url_policy",
            return_value=SimpleNamespace(
                private_network=False,
                hostname="example.invalid",
                addresses=("93.184.216.34",),
            ),
        )
        url_patch.start()
        self.addCleanup(url_patch.stop)

    async def test_progress_unknown_total_and_postprocessing(self) -> None:
        final = self.root / "abc-video.mp4"

        def behavior(ydl, _url, _download):
            final.write_bytes(b"video")
            ydl.opts["progress_hooks"][0](
                {
                    "status": "downloading",
                    "downloaded_bytes": 1234,
                    "total_bytes_estimate": None,
                    "speed": 500.5,
                    "eta": None,
                    "filename": str(final),
                }
            )
            ydl.opts["postprocessor_hooks"][0](
                {"status": "started", "filepath": str(final)}
            )
            return {
                "title": "video",
                "requested_downloads": [{"filepath": str(final)}],
            }

        _FakeYDL.behavior = behavior
        seen: list[DownloadProgress] = []
        with patch("src.downloader.YoutubeDL", _FakeYDL):
            result = await UrlDownloader().download(
                "https://example.invalid/watch?v=1",
                str(self.root),
                on_progress=seen.append,
            )
            await asyncio.sleep(0)

        self.assertEqual(result.path, str(final.resolve()))
        self.assertEqual(result.title, "video")
        self.assertEqual(seen[0].downloaded_bytes, 1234)
        self.assertIsNone(seen[0].total_bytes)
        self.assertEqual(seen[0].speed_bps, 500.5)
        self.assertEqual(seen[1].status, "postprocessing")
        self.assertEqual(seen[1].filename, final.name)

    async def test_existing_partial_is_not_cleared_and_explicit_output_wins(self) -> None:
        partial = self.root / "resume.part"
        partial.write_bytes(b"partial")
        final = self.root / "explicit.mp4"
        unrelated = self.root / "newer.tmp"

        def behavior(_ydl, _url, _download):
            unrelated.write_bytes(b"unrelated")
            final.write_bytes(b"final")
            os.utime(unrelated, (time.time() + 100, time.time() + 100))
            return {
                "title": "explicit",
                "requested_downloads": [{"filepath": str(final)}],
            }

        _FakeYDL.behavior = behavior
        with patch("src.downloader.YoutubeDL", _FakeYDL):
            result = await UrlDownloader().download("https://example.invalid/x", str(self.root))

        self.assertTrue(partial.exists())
        self.assertEqual(partial.read_bytes(), b"partial")
        self.assertEqual(result.path, str(final.resolve()))

    async def test_final_merged_path_wins_over_requested_fragments(self) -> None:
        video = self.root / "video-only.webm"
        audio = self.root / "audio-only.m4a"
        merged = self.root / "merged.mp4"
        video.write_bytes(b"video")
        audio.write_bytes(b"audio")
        merged.write_bytes(b"merged")

        def behavior(_ydl, _url, _download):
            return {
                "title": "merged",
                "filepath": str(merged),
                "requested_downloads": [
                    {"filepath": str(video)},
                    {"filepath": str(audio)},
                ],
            }

        _FakeYDL.behavior = behavior
        with patch("src.downloader.YoutubeDL", _FakeYDL):
            result = await UrlDownloader().download("https://example.invalid/m", str(self.root))

        self.assertEqual(result.path, str(merged.resolve()))

    async def test_output_path_escape_fails_closed(self) -> None:
        outside = self.root.parent / "escape.mp4"
        outside.write_bytes(b"escape")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))

        def behavior(_ydl, _url, _download):
            return {
                "title": "escape",
                "requested_downloads": [{"filepath": str(outside)}],
            }

        _FakeYDL.behavior = behavior
        with patch("src.downloader.YoutubeDL", _FakeYDL):
            with self.assertRaisesRegex(ValueError, "escaped job directory"):
                await UrlDownloader().download("https://example.invalid/x", str(self.root))

    async def test_cancellation_waits_until_worker_stops_writing(self) -> None:
        target = self.root / "stream.part"
        stopped = threading.Event()

        def behavior(ydl, _url, _download):
            try:
                for index in range(500):
                    with target.open("ab") as handle:
                        handle.write(b"x")
                    ydl.opts["progress_hooks"][0](
                        {
                            "status": "downloading",
                            "downloaded_bytes": index + 1,
                            "total_bytes": 500,
                            "filename": str(target),
                        }
                    )
                    time.sleep(0.005)
            finally:
                stopped.set()
            return {
                "title": "cancelled",
                "requested_downloads": [{"filepath": str(target)}],
            }

        _FakeYDL.behavior = behavior
        with patch("src.downloader.YoutubeDL", _FakeYDL):
            task = asyncio.create_task(
                UrlDownloader(cancel_wait_seconds=2).download(
                    "https://example.invalid/slow",
                    str(self.root),
                )
            )
            while not target.exists() or target.stat().st_size < 3:
                await asyncio.sleep(0.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(stopped.is_set())
        size = target.stat().st_size
        await asyncio.sleep(0.05)
        self.assertEqual(target.stat().st_size, size)
        self.assertLess(size, 500)

    async def test_pre_cancelled_token_never_starts_download(self) -> None:
        called = False

        def behavior(_ydl, _url, _download):
            nonlocal called
            called = True
            return {}

        _FakeYDL.behavior = behavior
        token = CancelToken()
        token.cancel()
        with patch("src.downloader.YoutubeDL", _FakeYDL):
            with self.assertRaises(Exception):
                await UrlDownloader().download(
                    "https://example.invalid/x", str(self.root), cancel_token=token
                )
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
