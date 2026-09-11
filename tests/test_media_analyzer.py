from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tgvio.application.media_analyzer import MediaAnalyzer
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class FakeInspector:
    async def inspect(self, item: MediaItem) -> MediaItem:
        from dataclasses import replace

        return replace(item, size_bytes=123, mime_type="video/mp4", container="mp4", codec="h264")


class MediaAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_analyzer_persists_facts_and_state(self) -> None:
        job = Job(
            owner_id=1,
            destination="@dest",
            state=JobState.DOWNLOADED,
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="x", local_path="/tmp/x.mp4")],
        )
        await self.repo.create(job)
        analyzed = await MediaAnalyzer(self.repo, FakeInspector()).analyze(job)
        self.assertEqual(analyzed.state, JobState.ANALYZED)
        loaded = await self.repo.get(job.id)
        self.assertEqual(loaded.state, JobState.ANALYZED)
        self.assertEqual(loaded.items[0].codec, "h264")
        events = await self.repo.list_events(job.id)
        self.assertEqual(
            [event.event_type for event in events],
            ["job_created", "analysis_started", "analysis_completed"],
        )

    async def test_analyzer_fails_job_when_inspector_raises(self) -> None:
        class BrokenInspector:
            async def inspect(self, item: MediaItem) -> MediaItem:
                raise RuntimeError("probe failed")

        job = Job(
            owner_id=1,
            destination="@dest",
            state=JobState.DOWNLOADED,
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="x", local_path="/tmp/x.mp4")],
        )
        await self.repo.create(job)
        with self.assertRaisesRegex(RuntimeError, "probe failed"):
            await MediaAnalyzer(self.repo, BrokenInspector()).analyze(job)
        loaded = await self.repo.get(job.id)
        self.assertEqual(loaded.state, JobState.FAILED)
        self.assertEqual(loaded.error_code, "media_analysis_failed")


class FFprobeInspectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_document_skips_ffprobe_but_keeps_file_facts(self) -> None:
        from tgvio.infrastructure.media_inspector import FFprobeMediaInspector

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.pdf"
            path.write_bytes(b"%PDF-1.4\nfixture\n")
            item = MediaItem(
                index=0,
                kind=MediaKind.DOCUMENT,
                source="fixture",
                local_path=str(path),
            )
            got = await FFprobeMediaInspector().inspect(item)
            self.assertEqual(got.kind, MediaKind.DOCUMENT)
            self.assertEqual(got.mime_type, "application/pdf")
            self.assertIsNone(got.container)
            self.assertIsNone(got.codec)
            self.assertTrue(got.sha256)
            self.assertTrue(got.metadata["probe_skipped"])
            self.assertTrue(got.metadata["send_as_document_candidate"])

    async def test_real_ffprobe_inspects_generated_mp4(self) -> None:
        from tgvio.infrastructure.media_inspector import FFprobeMediaInspector

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.mp4"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x48:d=0.2",
                "-an",
                "-c:v",
                "libx264",
                "-movflags",
                "+faststart",
                "-y",
                str(path),
            )
            self.assertEqual(await proc.wait(), 0)
            item = MediaItem(index=0, kind=MediaKind.DOCUMENT, source="fixture", local_path=str(path))
            got = await FFprobeMediaInspector().inspect(item)
            self.assertEqual(got.kind, MediaKind.VIDEO)
            self.assertEqual(got.container, "mov")
            self.assertEqual(got.codec, "h264")
            self.assertEqual((got.width, got.height), (64, 48))
            self.assertTrue(got.sha256)
            self.assertTrue(got.metadata["telegram_streamable_candidate"])
            self.assertTrue(got.metadata["faststart"])
            self.assertFalse(got.metadata["probe_skipped"])
