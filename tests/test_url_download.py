from __future__ import annotations

import asyncio
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest

from tgvio.adapters.url_downloader import UrlMediaDownloader
from tgvio.application.media_router import RoutedMediaDownloader
from tgvio.domain.job import MediaItem, MediaKind
from tgvio.infrastructure.url_security import validate_download_url, validate_url_syntax


def public_resolver(host: str, port: int, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def private_resolver(host: str, port: int, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


class FakeYdl:
    last_options = None

    def __init__(self, options) -> None:
        self.options = options
        FakeYdl.last_options = options
        self.path: Path | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url: str, download: bool):
        root = Path(self.options["outtmpl"]).parent
        self.path = root / "abc-demo.mp4"
        self.path.write_bytes(b"video-payload")
        for hook in self.options.get("progress_hooks") or []:
            hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 13})
            hook({"status": "finished", "downloaded_bytes": 13, "total_bytes": 13})
        return {
            "id": "abc",
            "title": "Demo title",
            "extractor_key": "Generic",
            "filepath": str(self.path),
        }

    def prepare_filename(self, info):
        assert self.path is not None
        return str(self.path)


class FailingYdl(FakeYdl):
    def extract_info(self, url: str, download: bool):
        raise RuntimeError(f"backend failed for {url}")


class URLSecurityTests(unittest.TestCase):
    def test_syntax_allows_normal_video_query_but_rejects_credentials(self) -> None:
        host, port = validate_url_syntax("https://example.test/watch?v=abc")
        self.assertEqual(host, "example.test")
        self.assertEqual(port, 443)
        with self.assertRaisesRegex(ValueError, "credential-like"):
            validate_url_syntax("https://example.test/file?token=secret")
        with self.assertRaisesRegex(ValueError, "credentials"):
            validate_url_syntax("https://alice:secret@example.test/file")

    def test_private_network_policy_blocks_local_resolution(self) -> None:
        with self.assertRaisesRegex(ValueError, "private/local"):
            validate_download_url(
                "https://example.test/video",
                resolver=private_resolver,
                private_network_policy="block",
            )
        risk = validate_download_url(
            "https://example.test/video",
            resolver=public_resolver,
            private_network_policy="block",
        )
        self.assertFalse(risk.private_network)


class UrlMediaDownloaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_returns_contained_durable_media_item(self) -> None:
        item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="url:https://example.com/watch?v=abc",
            metadata={"source_type": "url"},
        )
        downloader = UrlMediaDownloader(
            private_network_policy="allow",
            ydl_factory=FakeYdl,
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            progress = []
            result = await downloader.download(
                item,
                root,
                lambda current, total: progress.append((current, total)),
            )
            await asyncio.sleep(0)
            path = Path(result.local_path or "")
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, root.resolve())
            self.assertEqual(result.size_bytes, len(b"video-payload"))
            self.assertEqual(result.metadata["source_type"], "url")
            self.assertEqual(result.metadata["url_title"], "Demo title")
            self.assertEqual(result.metadata["url_hostname"], "example.com")
            self.assertTrue(FakeYdl.last_options["noplaylist"])
            self.assertEqual(progress, [(5, 13), (13, 13)])

    async def test_quality_preset_selects_a_bounded_format(self) -> None:
        item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="url:https://example.com/watch?v=abc",
            metadata={"source_type": "url", "ytdlp": {"preset": "720", "audio_only": False}},
        )
        downloader = UrlMediaDownloader(private_network_policy="allow", ydl_factory=FakeYdl)
        with TemporaryDirectory() as tmp:
            await downloader.download(item, Path(tmp))
        self.assertIn("height<=720", FakeYdl.last_options["format"])
        self.assertEqual(FakeYdl.last_options["merge_output_format"], "mp4")
        self.assertNotIn("postprocessors", FakeYdl.last_options)

    async def test_audio_only_extracts_mp3(self) -> None:
        item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="url:https://example.com/watch?v=abc",
            metadata={"source_type": "url", "ytdlp": {"preset": "best", "audio_only": True}},
        )
        downloader = UrlMediaDownloader(private_network_policy="allow", ydl_factory=FakeYdl)
        with TemporaryDirectory() as tmp:
            await downloader.download(item, Path(tmp))
        self.assertEqual(FakeYdl.last_options["format"], "bestaudio/best")
        postprocessors = FakeYdl.last_options["postprocessors"]
        self.assertEqual(postprocessors[0]["key"], "FFmpegExtractAudio")
        self.assertEqual(postprocessors[0]["preferredcodec"], "mp3")
        self.assertNotIn("merge_output_format", FakeYdl.last_options)

    async def test_cookies_file_is_only_used_when_present(self) -> None:
        item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="url:https://example.com/watch?v=abc",
            metadata={"source_type": "url"},
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookies = root / "cookies.txt"
            cookies.write_text("# netscape\n", encoding="utf-8")
            downloader = UrlMediaDownloader(
                private_network_policy="allow",
                ydl_factory=FakeYdl,
                cookies_file=str(cookies),
            )
            await downloader.download(item, root)
            self.assertEqual(FakeYdl.last_options["cookiefile"], str(cookies))

            missing = UrlMediaDownloader(
                private_network_policy="allow",
                ydl_factory=FakeYdl,
                cookies_file=str(root / "absent.txt"),
            )
            await missing.download(item, root)
            self.assertNotIn("cookiefile", FakeYdl.last_options)

    async def test_backend_exception_does_not_echo_source_url(self) -> None:
        item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="url:https://example.com/watch?v=private-value",
            metadata={"source_type": "url"},
        )
        downloader = UrlMediaDownloader(
            private_network_policy="allow",
            ydl_factory=FailingYdl,
        )
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "URL download failed") as ctx:
                await downloader.download(item, Path(tmp))
        self.assertNotIn("private-value", str(ctx.exception))

    async def test_router_uses_url_route_without_telegram_source_ids(self) -> None:
        class DefaultDownloader:
            async def download(self, item, target_dir, progress_callback=None):
                raise AssertionError("default downloader should not be used")

        item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="url:https://example.com/demo",
            metadata={"source_type": "url"},
        )
        router = RoutedMediaDownloader(
            DefaultDownloader(),
            {"url": UrlMediaDownloader(private_network_policy="allow", ydl_factory=FakeYdl)},
        )
        with TemporaryDirectory() as tmp:
            result = await router.download(item, Path(tmp))
        self.assertTrue(result.metadata["download_complete"])
