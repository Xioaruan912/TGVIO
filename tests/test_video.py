import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.media import MediaDownloader
from src.services.media_compat import MediaCompatibilityManager
from src.video import _run_media_process, make_thumb, probe_media_metadata, remux_faststart


class VideoMetadataTests(unittest.IsolatedAsyncioTestCase):
    def _make_mp4(self, path: Path) -> None:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10",
                "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.fail(result.stderr[-1000:])

    async def test_probe_and_faststart_remux_preserve_media_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.mp4"
            self._make_mp4(source)
            before = await probe_media_metadata(str(source))
            output = await remux_faststart(str(source))
            after = await probe_media_metadata(output)

            self.assertEqual(before.video_codec, "h264")
            self.assertEqual((before.width, before.height), (160, 90))
            self.assertGreater(before.duration_seconds, 0)
            self.assertEqual(before.stream_count, after.stream_count)
            self.assertAlmostEqual(before.duration_seconds, after.duration_seconds, delta=1.0)
            self.assertTrue(after.faststart)
            self.assertTrue(after.telegram_streaming_ready)
            self.assertTrue(os.path.isfile(output))

    async def test_remux_refuses_output_outside_job_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.mp4"
            source.write_bytes(b"not-a-real-video")
            with self.assertRaises(ValueError):
                await remux_faststart(str(source), str(root.parent / "escape.mp4"))

    async def test_thumbnail_auto_skips_black_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.mp4"
            self._make_mp4(source)
            with patch("src.video._thumbnail_seeks", new=AsyncMock(return_value=[0.1, 0.2])), patch(
                "src.video._frame_is_black", new=AsyncMock(side_effect=[True, False])
            ) as black:
                thumb = await make_thumb(str(source), str(root), position="auto")
            self.assertTrue(thumb)
            self.assertTrue(Path(thumb).exists())
            self.assertEqual(black.call_count, 2)

    async def test_media_process_cancellation_kills_child(self) -> None:
        task = asyncio.create_task(
            _run_media_process(
                ["python", "-c", "import time; time.sleep(30)"],
                timeout=60,
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    async def test_media_process_concurrency_is_bounded_to_two(self) -> None:
        active = 0
        peak = 0
        release = asyncio.Event()

        class _Process:
            returncode = None

            async def communicate(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    await release.wait()
                finally:
                    active -= 1
                self.returncode = 0
                return b"", b""

            def kill(self):
                self.returncode = -9

        async def _spawn(*_args, **_kwargs):
            return _Process()

        with patch("src.video.asyncio.create_subprocess_exec", new=_spawn):
            tasks = [
                asyncio.create_task(_run_media_process(["ffprobe"], timeout=5))
                for _ in range(3)
            ]
            await asyncio.sleep(0.05)
            self.assertEqual(peak, 2)
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(peak, 2)


class PostDownloadHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_download_hook_can_replace_paths_for_later_hooks(self) -> None:
        downloader = MediaDownloader(
            SimpleNamespace(),
            lambda _seq: "/tmp",
            download_timeout=30,
        )
        downloader._download = AsyncMock(return_value="/tmp/original.mp4")
        seen = []

        async def replace(_job, paths):
            self.assertEqual(paths, "/tmp/original.mp4")
            return "/tmp/remuxed.mp4"

        async def observe(_job, paths):
            seen.append(paths)

        downloader.post_download_hooks.extend((replace, observe))
        result = await downloader.run(SimpleNamespace(seq=1))

        self.assertEqual(result, "/tmp/remuxed.mp4")
        self.assertEqual(seen, ["/tmp/remuxed.mp4"])


class MediaCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def _make_mp4(self, path: Path) -> None:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=red:s=160x90:r=10",
                "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.fail(result.stderr[-1000:])

    async def test_analyze_mode_preserves_original_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.mp4"
            self._make_mp4(source)
            job = SimpleNamespace(seq=41)
            manager = MediaCompatibilityManager(mode="analyze")
            result = await manager.process(job, str(source))
            self.assertEqual(result, str(source))
            self.assertTrue(job._media_metadata)
            self.assertTrue(job._media_compat_notes)

    async def test_remux_mode_replaces_path_and_restores_reservation(self) -> None:
        class _Disk:
            def __init__(self):
                self.value = 100
                self.calls = []

            def reserved_bytes(self, _seq):
                return self.value

            def reserve(self, _seq, value):
                self.calls.append(value)
                self.value = value
                return SimpleNamespace(healthy=True)

            def release(self, _seq):
                self.value = 0

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.mp4"
            self._make_mp4(source)
            before = await probe_media_metadata(str(source))
            self.assertTrue(before.needs_faststart)
            disk = _Disk()
            manager = MediaCompatibilityManager(mode="remux", disk=disk)
            job = SimpleNamespace(seq=42)
            result = await manager.process(job, str(source))
            self.assertNotEqual(result, str(source))
            self.assertTrue(Path(result).exists())
            self.assertEqual(disk.value, 100)
            self.assertGreater(max(disk.calls), 100)
            self.assertIn("已完成 faststart", " ".join(job._media_compat_notes))

    async def test_low_disk_skips_optional_remux(self) -> None:
        class _Disk:
            def __init__(self):
                self.value = 200

            def reserved_bytes(self, _seq):
                return self.value

            def reserve(self, _seq, value):
                self.value = value
                return SimpleNamespace(healthy=value <= 200)

            def release(self, _seq):
                self.value = 0

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.mp4"
            self._make_mp4(source)
            disk = _Disk()
            manager = MediaCompatibilityManager(mode="remux", disk=disk)
            job = SimpleNamespace(seq=43)
            result = await manager.process(job, str(source))
            self.assertEqual(result, str(source))
            self.assertEqual(disk.value, 200)
            self.assertIn("磁盘余量不足", " ".join(job._media_compat_notes))


if __name__ == "__main__":
    unittest.main()
