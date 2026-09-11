from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime
from tgvio.adapters.telegram.media_downloader import TelethonMediaDownloader
from tgvio.domain.job import MediaItem, MediaKind


class TelegramIntakeMappingTests(unittest.TestCase):
    def test_video_message_maps_without_telethon_objects_leaking_into_domain(self) -> None:
        message = SimpleNamespace(
            id=77,
            photo=None,
            video=object(),
            document=object(),
            file=SimpleNamespace(name="movie.mp4", ext=".mp4", size=1234),
            media=SimpleNamespace(spoiler=True),
            message="caption",
            grouped_id=900,
        )
        incoming = TelethonIntakeRuntime._from_message(message, 42)
        self.assertIsNotNone(incoming)
        assert incoming is not None
        self.assertEqual(incoming.kind, MediaKind.VIDEO)
        self.assertEqual(incoming.source, "telegram:42:77")
        self.assertEqual(incoming.source_chat_id, 42)
        self.assertEqual(incoming.source_message_id, 77)
        self.assertEqual(incoming.size_bytes, 1234)
        self.assertTrue(incoming.spoiler)
        self.assertEqual(incoming.metadata["telegram_original_name"], "movie.mp4")

    def test_plain_text_is_not_telegram_media(self) -> None:
        message = SimpleNamespace(
            id=78,
            photo=None,
            video=None,
            document=None,
        )
        self.assertIsNone(TelethonIntakeRuntime._from_message(message, 42))

    def test_url_text_maps_to_durable_url_source_without_telethon_objects(self) -> None:
        message = SimpleNamespace(
            id=79,
            photo=None,
            video=None,
            document=None,
            message="https://example.com/watch?v=abc",
        )
        incoming = TelethonIntakeRuntime._from_url_message(message, 42)
        self.assertIsNotNone(incoming)
        assert incoming is not None
        self.assertEqual(incoming.kind, MediaKind.DOCUMENT)
        self.assertEqual(incoming.source, "url:https://example.com/watch?v=abc")
        self.assertEqual(incoming.metadata["source_type"], "url")
        self.assertEqual(incoming.metadata["url_hostname"], "example.com")
        self.assertEqual(incoming.source_message_id, 79)

    def test_url_with_credential_query_is_rejected_before_persistence(self) -> None:
        message = SimpleNamespace(
            id=80,
            photo=None,
            video=None,
            document=None,
            message="https://example.com/file?token=secret",
        )
        with self.assertRaisesRegex(ValueError, "credential-like"):
            TelethonIntakeRuntime._from_url_message(message, 42)


@dataclass
class FakeTelegramFile:
    name: str | None = "video.mp4"
    ext: str = ".mp4"
    size: int = 7


class FakeTelegramClient:
    def __init__(self) -> None:
        self.download_calls = 0
        self.message = SimpleNamespace(media=object(), file=FakeTelegramFile())

    async def get_messages(self, chat_id, ids):
        return self.message

    async def download_media(self, message, file, progress_callback=None):
        self.download_calls += 1
        if progress_callback is not None:
            progress_callback(3, 7)
            progress_callback(7, 7)
        Path(file).write_bytes(b"payload")
        return file


class TelethonMediaDownloaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_is_atomic_and_reuses_complete_local_file(self) -> None:
        client = FakeTelegramClient()
        downloader = TelethonMediaDownloader(client)
        item = MediaItem(
            index=0,
            kind=MediaKind.VIDEO,
            source="telegram:42:99",
            size_bytes=7,
            source_chat_id=42,
            source_message_id=99,
        )
        with TemporaryDirectory() as tmp:
            progress = []
            first = await downloader.download(
                item,
                Path(tmp),
                lambda current, total: progress.append((current, total)),
            )
            self.assertEqual(client.download_calls, 1)
            self.assertEqual(progress, [(3, 7), (7, 7)])
            self.assertTrue(Path(first.local_path).is_file())
            self.assertEqual(Path(first.local_path).read_bytes(), b"payload")
            self.assertFalse(first.metadata["download_reused_local"])

            second = await downloader.download(item, Path(tmp))
            self.assertEqual(client.download_calls, 1)
            self.assertTrue(second.metadata["download_reused_local"])
