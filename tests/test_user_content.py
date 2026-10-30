from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tgvio.application.content_prefs import (
    content_policy_snapshot,
)
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
from tgvio.domain.job import Job, MediaItem, MediaKind
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


class ContentMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

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
            self.assertGreaterEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                17,
            )
        finally:
            connection.close()

    def test_snapshot_falls_back_to_defaults(self) -> None:
        from tgvio.domain.intake import UserPreference

        snapshot = content_policy_snapshot(UserPreference(owner_id=1))
        self.assertEqual(snapshot["ytdlp"], {"preset": "best", "audio_only": False})
        self.assertNotIn("thumbnail_path", snapshot)


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
