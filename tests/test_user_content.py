from __future__ import annotations

import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path

from tgvio.application.content_prefs import (
    ContentPreferenceService,
    content_policy_snapshot,
)
from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.media_downloader import JobDownloader
from tgvio.domain.content import (
    AUDIO_ONLY_PRESET,
    ContentPreferenceError,
    YTDLP_PRESETS,
    normalize_ytdlp_preset,
    render_caption_template,
    split_template_buttons,
    validate_caption_template,
    ytdlp_format_for,
)
from tgvio.domain.intake import SpoilerMode
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.infrastructure.media_inspector import FFprobeMediaInspector
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class ContentDomainTests(unittest.TestCase):
    def test_preset_normalization_and_formats(self) -> None:
        self.assertEqual(normalize_ytdlp_preset(None), "best")
        self.assertEqual(normalize_ytdlp_preset(" AUDIO "), AUDIO_ONLY_PRESET)
        self.assertEqual(ytdlp_format_for("best"), YTDLP_PRESETS["best"])
        self.assertEqual(ytdlp_format_for(AUDIO_ONLY_PRESET), "bestaudio/best")
        with self.assertRaises(ContentPreferenceError):
            normalize_ytdlp_preset("4k")

    def test_template_validation_rejects_unknown_variables_and_length(self) -> None:
        self.assertEqual(validate_caption_template(" {channel} {date} "), "{channel} {date}")
        with self.assertRaises(ContentPreferenceError):
            validate_caption_template("{unknown}")
        with self.assertRaises(ContentPreferenceError):
            validate_caption_template("x" * 500)

    def test_render_substitutes_known_variables(self) -> None:
        self.assertEqual(
            render_caption_template(
                "{channel} {date} {index}/{total} {missing}",
                {"channel": "@c", "date": "2026-09-15", "index": "1", "total": "3"},
            ),
            "@c 2026-09-15 1/3",
        )

    def test_button_lines_are_parsed_and_validated(self) -> None:
        text, buttons = split_template_buttons(
            "正文\nbutton: 打开 | https://t.me/x\n尾行"
        )
        self.assertEqual(text, "正文\n尾行")
        self.assertEqual(buttons, (("打开", "https://t.me/x"),))
        with self.assertRaises(ContentPreferenceError):
            split_template_buttons("button: 坏 | ftp://example.test/x")


class ContentPreferenceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.service = ContentPreferenceService(self.repo)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_migration_17_adds_content_columns(self) -> None:
        connection = sqlite3.connect(Path(self.tmp.name) / "state.sqlite3")
        try:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(user_preferences)")
            }
            self.assertLessEqual(
                {
                    "thumbnail_path",
                    "caption_template",
                    "ytdlp_preset",
                    "ytdlp_audio_only",
                },
                columns,
            )
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                17,
            )
        finally:
            connection.close()

    async def test_preferences_round_trip_and_clear(self) -> None:
        await self.service.set_thumbnail(7, "/data/content/thumbnail-7.jpg")
        await self.service.set_caption_template(7, "{channel} {date}")
        await self.service.set_ytdlp(7, preset="720", audio_only=True)

        stored = await self.service.current(7)
        self.assertEqual(stored.thumbnail_path, "/data/content/thumbnail-7.jpg")
        self.assertEqual(stored.caption_template, "{channel} {date}")
        self.assertEqual(stored.ytdlp_preset, "720")
        self.assertTrue(stored.ytdlp_audio_only)

        await self.service.clear_thumbnail(7)
        await self.service.clear_caption_template(7)
        cleared = await self.service.current(7)
        self.assertIsNone(cleared.thumbnail_path)
        self.assertIsNone(cleared.caption_template)

    async def test_invalid_template_is_rejected_before_persisting(self) -> None:
        with self.assertRaises(ContentPreferenceError):
            await self.service.set_caption_template(7, "{nope}")
        self.assertIsNone((await self.service.current(7)).caption_template)

    async def test_partial_ytdlp_update_keeps_other_fields(self) -> None:
        await self.service.set_ytdlp(7, preset="480", audio_only=False)
        await self.service.set_ytdlp(7, audio_only=True)
        stored = await self.service.current(7)
        self.assertEqual(stored.ytdlp_preset, "480")
        self.assertTrue(stored.ytdlp_audio_only)

    def test_snapshot_falls_back_to_defaults(self) -> None:
        from tgvio.domain.intake import UserPreference

        snapshot = content_policy_snapshot(UserPreference(owner_id=1))
        self.assertEqual(snapshot["ytdlp"], {"preset": "best", "audio_only": False})
        self.assertNotIn("thumbnail_path", snapshot)


class IntakeContentSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.service = ContentPreferenceService(self.repo)
        self.intake = IntakeService(self.repo)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_accepted_job_freezes_content_preferences(self) -> None:
        await self.service.set_thumbnail(9, "/data/content/thumbnail-9.jpg")
        await self.service.set_caption_template(9, "{channel} {index}")
        await self.service.set_ytdlp(9, preset="1080", audio_only=True)

        job = await self.intake.accept(
            owner_id=9,
            destination="@channel",
            spoiler_mode=SpoilerMode.ALWAYS_NORMAL,
            media=[
                IncomingMedia(
                    kind=MediaKind.DOCUMENT,
                    source="url:https://example.test/v",
                    metadata={"source_type": "url"},
                    source_chat_id=9,
                    source_message_id=1,
                )
            ],
        )
        self.assertEqual(job.policy["thumbnail_path"], "/data/content/thumbnail-9.jpg")
        self.assertEqual(job.policy["caption_template"], "{channel} {index}")
        self.assertEqual(
            job.policy["ytdlp"],
            {"preset": "1080", "audio_only": True},
        )

    async def test_explicit_policy_wins_over_snapshot(self) -> None:
        await self.service.set_ytdlp(9, preset="1080", audio_only=False)
        job = await self.intake.accept(
            owner_id=9,
            destination="@channel",
            policy={"ytdlp": {"preset": "480", "audio_only": True}},
            spoiler_mode=SpoilerMode.ALWAYS_NORMAL,
            media=[
                IncomingMedia(
                    kind=MediaKind.DOCUMENT,
                    source="url:https://example.test/v",
                    metadata={"source_type": "url"},
                    source_chat_id=9,
                    source_message_id=2,
                )
            ],
        )
        self.assertEqual(job.policy["ytdlp"], {"preset": "480", "audio_only": True})


class UrlOptionsMergeTests(unittest.TestCase):
    def test_url_item_receives_frozen_policy(self) -> None:
        item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="url:https://example.test/v",
            metadata={"source_type": "url"},
        )
        job = Job(
            owner_id=1,
            destination="@channel",
            items=[item],
            policy={"ytdlp": {"preset": "720", "audio_only": True}},
        )
        merged = JobDownloader._with_url_options(job, item)
        self.assertEqual(
            merged.metadata["ytdlp"],
            {"preset": "720", "audio_only": True},
        )

    def test_telegram_item_and_missing_policy_are_untouched(self) -> None:
        telegram_item = MediaItem(
            index=0,
            kind=MediaKind.VIDEO,
            source="telegram:1:2",
            metadata={},
        )
        job = Job(
            owner_id=1,
            destination="@channel",
            items=[telegram_item],
            policy={"ytdlp": {"preset": "720", "audio_only": False}},
        )
        self.assertIs(JobDownloader._with_url_options(job, telegram_item), telegram_item)

        url_item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="url:https://example.test/v",
            metadata={"source_type": "url"},
        )
        plain_job = Job(owner_id=1, destination="@channel", items=[url_item])
        self.assertIs(JobDownloader._with_url_options(plain_job, url_item), url_item)


class ContentUIPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_content_page_renders_state_and_short_callbacks(self) -> None:
        from tgvio.adapters.telegram.bot_ui_content import BotUIContentMixin

        class _UI(BotUIContentMixin):
            def __init__(self, repository, settings):
                self._repository = repository
                self._settings = settings

            async def _edit_page(self, event, text, buttons):
                event.edits.append((text, buttons))

            async def _safe_answer(self, event, text=None, **kwargs):
                event.answers.append(text)

        class _Event:
            def __init__(self):
                self.edits = []
                self.answers = []

        ui = _UI(self.repo, SimpleNamespace(data_dir=self.tmp.name, ytdlp_cookies_file=""))
        event = _Event()
        text, rows = await ui._content_page(7)
        self.assertIn("内容与下载", text)
        self.assertIn("最佳画质", text)
        encoded = [
            button.data
            for row in rows
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertTrue(encoded)
        self.assertTrue(all(len(data) <= 64 for data in encoded))

        await ui._content_ytdlp_callback(event, 7, "720")
        self.assertEqual((await ui._content_service().current(7)).ytdlp_preset, "720")

        await ui._content_audio_toggle_callback(event, 7)
        self.assertTrue((await ui._content_service().current(7)).ytdlp_audio_only)

    async def test_settings_page_exposes_content_entry(self) -> None:
        from tgvio.adapters.telegram.bot_ui_content import BotUIContentMixin
        from tgvio.adapters.telegram.bot_ui_format import BotUIFormatMixin

        class _UI(BotUIFormatMixin, BotUIContentMixin):
            def __init__(self, repository, settings):
                self._repository = repository
                self._settings = settings
                self._runtime_flags = None

        ui = _UI(self.repo, SimpleNamespace(archive_layout="v2"))
        buttons = ui._settings_page_buttons(False)
        data = {
            button.data
            for row in buttons
            for button in row
            if getattr(button, "data", None)
        }
        self.assertIn(b"ui:content", data)


class ContentCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    def _ui(self, payload: bytes):
        from tgvio.adapters.telegram.bot_ui_content import BotUIContentMixin

        class _Client:
            async def download_media(self, message, file=None):
                Path(file).write_bytes(payload)
                return str(file)

        class _Reply:
            def __init__(self):
                self.photo = object()
                self.document = None
                self.raw_text = ""

        class _Event:
            def __init__(self):
                self.responses: list[str] = []

            async def get_reply_message(self):
                return _Reply()

            async def respond(self, text, **_kwargs):
                self.responses.append(text)

        class _UI(BotUIContentMixin):
            def __init__(self, repository, settings, client):
                self._repository = repository
                self._settings = settings
                self._client = client

        settings = SimpleNamespace(data_dir=self.tmp.name)
        return _UI(self.repo, settings, _Client()), _Event()

    async def test_thumb_command_stores_normalized_owner_thumbnail(self) -> None:
        ui, event = self._ui(b"image-bytes")
        await ui._set_thumbnail_from_message(event, 7)
        stored = await ui._content_service().current(7)
        self.assertIsNotNone(stored.thumbnail_path)
        self.assertTrue(Path(stored.thumbnail_path).is_file())
        self.assertTrue(any("已设置" in text for text in event.responses))

    async def test_thumb_command_rejects_oversized_image(self) -> None:
        ui, event = self._ui(b"x" * (5 * 1024 * 1024 + 1))
        await ui._set_thumbnail_from_message(event, 7)
        self.assertIsNone((await ui._content_service().current(7)).thumbnail_path)
        self.assertTrue(any("5 MB" in text for text in event.responses))

    async def test_caption_command_accepts_argument_and_validates(self) -> None:
        ui, event = self._ui(b"image-bytes")
        await ui._set_caption_template_from_message(event, 7, "{channel}\\n第{index}集")
        self.assertEqual(
            (await ui._content_service().current(7)).caption_template,
            "{channel}\n第{index}集",
        )
        await ui._set_caption_template_from_message(event, 7, "{bogus}")
        self.assertTrue(any("模板无效" in text for text in event.responses))
        self.assertEqual(
            (await ui._content_service().current(7)).caption_template,
            "{channel}\n第{index}集",
        )

class MediaInspectorAudioTests(unittest.TestCase):
    def test_audio_inference_and_document_candidate(self) -> None:
        kind = FFprobeMediaInspector._infer_kind(
            Path("song.mp3"),
            "audio/mpeg",
            None,
            {"codec_type": "audio"},
        )
        self.assertEqual(kind, MediaKind.AUDIO)
        by_mime = FFprobeMediaInspector._infer_kind(
            Path("unknown.bin"),
            "audio/mpeg",
            None,
            None,
        )
        self.assertEqual(by_mime, MediaKind.AUDIO)
        self.assertEqual(
            FFprobeMediaInspector._infer_kind(Path("a.mp4"), "video/mp4", {}, None),
            MediaKind.VIDEO,
        )
        self.assertEqual(
            FFprobeMediaInspector._infer_kind(Path("a.jpg"), "image/jpeg", None, None),
            MediaKind.PHOTO,
        )
        self.assertEqual(
            FFprobeMediaInspector._infer_kind(Path("a.zip"), "application/zip", None, None),
            MediaKind.DOCUMENT,
        )
