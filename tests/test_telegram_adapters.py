from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime
from tgvio.adapters.telegram.media_downloader import TelethonMediaDownloader
from tgvio.application.auto_recovery import AutoRecoveryPolicy
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind


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

    def test_failed_live_status_uses_plain_language_and_direct_retry_button(self) -> None:
        runtime = object.__new__(TelethonIntakeRuntime)
        runtime._settings = SimpleNamespace(publish_enabled=True)
        failed = Job(
            id="d" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.FAILED,
            error_code="download_failed",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture", size_bytes=7)],
        )

        text = runtime._render_live_status(failed, None, None, accepted_order=24)
        buttons = runtime._status_buttons(failed, None)

        self.assertIn("任务 #24", text)
        self.assertNotIn(failed.id[:10], text)
        self.assertIn("暂时无法读取原媒体", text)
        self.assertIn("尚未向目标频道发布", text)
        self.assertNotIn("download_failed", text)
        payloads = [button.data for row in buttons for button in row]
        self.assertIn(f"ui:retry:{failed.id}".encode(), payloads)

    def test_archive_failure_status_does_not_describe_telegram_publish_as_failed(self) -> None:
        runtime = object.__new__(TelethonIntakeRuntime)
        runtime._settings = SimpleNamespace(publish_enabled=True)
        completed = Job(
            id="e" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture", size_bytes=7)],
        )
        archive = ArchivePackage(
            id=f"arc_{completed.id}",
            job_id=completed.id,
            layout_version="v1",
            remote_path="archive/x",
            staging_path=".staging/x",
            state=ArchivePackageState.FAILED,
            manifest={},
            objects=(),
            error_code="archive_execution_failed",
        )

        text = runtime._render_live_status(completed, None, archive)
        buttons = runtime._status_buttons(completed, archive)

        self.assertIn("Telegram 发布完成", text)
        self.assertIn("Telegram 发布不受影响", text)
        self.assertNotIn("archive_execution_failed", text)
        payloads = [button.data for row in buttons for button in row]
        self.assertIn(f"ui:archive-retry:{completed.id}".encode(), payloads)

    def test_auto_retry_status_needs_no_retry_button_and_tracker_stays_alive(self) -> None:
        runtime = object.__new__(TelethonIntakeRuntime)
        runtime._settings = SimpleNamespace(publish_enabled=True)
        failed = Job(
            id="f" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.FAILED,
            error_code="download_failed",
            policy={
                "auto_recovery": AutoRecoveryPolicy().frozen(),
                "auto_recovery_job": {
                    "status": "scheduled",
                    "next_attempt": 1,
                    "max_attempts": 3,
                },
            },
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )

        text = runtime._render_live_status(failed, None, None)
        buttons = runtime._status_buttons(failed, None)

        self.assertIn("自动恢复", text)
        self.assertIn("无需操作", text)
        payloads = [button.data for row in buttons for button in row]
        self.assertNotIn(f"ui:retry:{failed.id}".encode(), payloads)
        self.assertFalse(runtime._status_is_terminal(failed, None))

    def test_uncertain_publish_quarantine_is_terminal_but_never_retryable(self) -> None:
        runtime = object.__new__(TelethonIntakeRuntime)
        runtime._settings = SimpleNamespace(publish_enabled=True)
        failed = Job(
            id="a" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.FAILED,
            error_code="publish_uncertain",
            policy={
                "auto_recovery": AutoRecoveryPolicy().frozen(),
                "auto_recovery_job": {"status": "quarantined"},
            },
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )

        text = runtime._render_live_status(failed, None, None)
        buttons = runtime._status_buttons(failed, None)

        self.assertIn("已隔离", text)
        self.assertIn("后续任务会继续", text)
        payloads = [button.data for row in buttons for button in row]
        self.assertNotIn(f"ui:retry:{failed.id}".encode(), payloads)
        self.assertTrue(runtime._status_is_terminal(failed, None))


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
    async def test_preview_stream_rejects_chunk_before_budget_overflow(self):
        class Client:
            maximum_written = 0
            async def get_messages(self, *args, **kwargs):
                return SimpleNamespace(media=object(), file=SimpleNamespace(size=0))
            async def iter_download(self, *args, **kwargs):
                yield b"12345678"
                self.maximum_written = path.stat().st_size if path.exists() else 0
                yield b"abcdefgh"
                raise AssertionError("must not request another chunk after budget exceeded")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "preview-source.jpg"
            client = Client()
            item = MediaItem(index=0, kind=MediaKind.PHOTO, source="fixture",
                             source_chat_id=42, source_message_id=1)
            with self.assertRaises(ValueError):
                await TelethonMediaDownloader(client).download_bounded(item, root, max_bytes=10)
            self.assertLessEqual(client.maximum_written, 10)
            self.assertFalse(path.exists())

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

    async def test_exhausted_concurrent_download_falls_back_to_one_stream(self) -> None:
        class FailingConcurrentClient(FakeTelegramClient):
            def __init__(self) -> None:
                super().__init__()
                self.concurrent_calls = 0

            def iter_download(self, *args, **kwargs):
                async def chunks_iterator():
                    self.concurrent_calls += 1
                    raise ValueError("fixture shard failure")
                    yield b""  # pragma: no cover

                return chunks_iterator()

        client = FailingConcurrentClient()
        downloader = TelethonMediaDownloader(
            client,
            download_workers=2,
            shard_retries=0,
        )
        item = MediaItem(
            index=0,
            kind=MediaKind.VIDEO,
            source="telegram:42:99",
            size_bytes=7,
            source_chat_id=42,
            source_message_id=99,
        )

        with TemporaryDirectory() as tmp:
            completed = await downloader.download(item, Path(tmp))

        self.assertGreater(client.concurrent_calls, 0)
        self.assertEqual(client.download_calls, 1)
        self.assertEqual(completed.size_bytes, 7)

    async def test_cancelled_concurrent_download_never_starts_fallback(self) -> None:
        class CancelledConcurrentClient(FakeTelegramClient):
            def iter_download(self, *args, **kwargs):
                async def cancel():
                    raise asyncio.CancelledError
                    yield b""  # pragma: no cover

                return cancel()

        client = CancelledConcurrentClient()
        downloader = TelethonMediaDownloader(
            client,
            download_workers=2,
            shard_retries=0,
        )
        item = MediaItem(
            index=0,
            kind=MediaKind.VIDEO,
            source="telegram:42:99",
            size_bytes=7,
            source_chat_id=42,
            source_message_id=99,
        )

        with TemporaryDirectory() as tmp:
            with self.assertRaises(asyncio.CancelledError):
                await downloader.download(item, Path(tmp))

        self.assertEqual(client.download_calls, 0)
