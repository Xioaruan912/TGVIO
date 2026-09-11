from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.progress import JobProgress
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class SQLiteJobRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_create_round_trips_structured_media_and_event(self) -> None:
        job = Job(
            owner_id=7,
            destination="@channel",
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.VIDEO,
                    source="tg:123:456",
                    caption="hello",
                    local_path="/cache/a.mp4",
                    name="a.mp4",
                    size_bytes=123456,
                    mime_type="video/mp4",
                    width=1920,
                    height=1080,
                    duration_seconds=9.5,
                    container="mov,mp4",
                    codec="h264",
                    spoiler=True,
                    grouped_id=88,
                    source_chat_id=-1001,
                    source_message_id=456,
                    sha256="abc",
                    telegram_ref="doc:ref",
                    metadata={"faststart": True},
                )
            ],
        )
        await self.repo.create(job)

        loaded = await self.repo.get(job.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.state, JobState.RECEIVED)
        self.assertEqual(loaded.items, job.items)

        events = await self.repo.list_events(job.id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "job_created")
        self.assertIsNone(events[0].from_state)
        self.assertEqual(events[0].to_state, JobState.RECEIVED)
        progress = await self.repo.get_job_progress(job.id)
        assert progress is not None
        self.assertEqual(progress.phase, "queued")
        await self.repo.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="downloading",
                current=50,
                total=100,
                item_index=0,
                item_total=1,
            )
        )
        progress = await self.repo.get_job_progress(job.id)
        assert progress is not None
        self.assertEqual(progress.current, 50)
        self.assertEqual(progress.total, 100)

    async def test_legal_lifecycle_is_durable_and_auditable(self) -> None:
        job = Job(owner_id=7, destination="@channel", items=[])
        await self.repo.create(job)

        transitions = [
            (JobState.DOWNLOADED, "download_skipped"),
            (JobState.ANALYZING, "analysis_started"),
            (JobState.ANALYZED, "analysis_completed"),
            (JobState.PLANNED, "plan_created"),
            (JobState.PUBLISHING, "publish_started"),
            (JobState.SUCCEEDED, "publish_completed"),
        ]
        for state, event_type in transitions:
            loaded = await self.repo.transition(job.id, state, event_type=event_type)
            self.assertEqual(loaded.state, state)

        events = await self.repo.list_events(job.id)
        self.assertEqual(
            [event.event_type for event in events],
            ["job_created"] + [event for _, event in transitions],
        )
        self.assertEqual(events[-1].to_state, JobState.SUCCEEDED)

    async def test_illegal_transition_does_not_mutate_job_or_events(self) -> None:
        job = Job(owner_id=7, destination="@channel", items=[])
        await self.repo.create(job)

        with self.assertRaisesRegex(ValueError, "illegal transition"):
            await self.repo.transition(
                job.id,
                JobState.PUBLISHING,
                event_type="invalid_shortcut",
            )

        loaded = await self.repo.get(job.id)
        assert loaded is not None
        self.assertEqual(loaded.state, JobState.RECEIVED)
        self.assertEqual(len(await self.repo.list_events(job.id)), 1)

    async def test_failed_transition_persists_error_without_secret_requirement(self) -> None:
        job = Job(owner_id=7, destination="@channel", items=[])
        await self.repo.create(job)
        failed = await self.repo.transition(
            job.id,
            JobState.FAILED,
            event_type="intake_failed",
            detail="validation",
            error_code="invalid_media",
            error_message="unsupported payload",
        )
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.error_code, "invalid_media")
        self.assertEqual(failed.error_message, "unsupported payload")
        self.assertTrue(failed.terminal)

    async def test_duplicate_item_indexes_are_rejected_atomically(self) -> None:
        job = Job(
            owner_id=7,
            destination="@channel",
            items=[
                MediaItem(index=0, kind=MediaKind.PHOTO, source="a"),
                MediaItem(index=0, kind=MediaKind.PHOTO, source="b"),
            ],
        )
        with self.assertRaises(Exception):
            await self.repo.create(job)
        self.assertIsNone(await self.repo.get(job.id))

    async def test_list_by_states_returns_only_recoverable_jobs(self) -> None:
        received = Job(owner_id=7, destination="@channel", items=[])
        analyzed = Job(owner_id=7, destination="@channel", items=[])
        await self.repo.create(received)
        await self.repo.create(analyzed)
        analyzed = await self.repo.transition(
            analyzed.id,
            JobState.DOWNLOADED,
            event_type="download_skipped",
        )
        analyzed = await self.repo.transition(
            analyzed.id,
            JobState.ANALYZING,
            event_type="analysis_started",
        )
        await self.repo.transition(
            analyzed.id,
            JobState.ANALYZED,
            event_type="analysis_completed",
        )

        rows = await self.repo.list_by_states(
            (JobState.RECEIVED, JobState.DOWNLOADING, JobState.DOWNLOADED, JobState.ANALYZING)
        )
        self.assertEqual([job.id for job in rows], [received.id])

    async def test_stats_and_event_summary_are_owner_scoped(self) -> None:
        own = Job(
            owner_id=42,
            destination="@channel",
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="a", size_bytes=10)],
        )
        foreign = Job(
            owner_id=99,
            destination="@channel",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="b", size_bytes=20)],
        )
        await self.repo.create(own)
        await self.repo.create(foreign)
        await self.repo.transition(
            own.id,
            JobState.FAILED,
            event_type="forced_failure",
            error_code="test",
        )
        stats = await self.repo.get_stats_snapshot(owner_id=42)
        self.assertEqual(stats["jobs_total"], 1)
        self.assertEqual(stats["media_items"], 1)
        self.assertEqual(stats["media_bytes"], 10)
        self.assertEqual(stats["failed"], 1)
        events = await self.repo.get_recent_event_counts(owner_id=42, hours=24)
        self.assertEqual(events["job_created"], 1)
        self.assertEqual(events["forced_failure"], 1)
        self.assertNotIn("telegram_cache_entries", stats)
        self.assertTrue(await self.repo.quick_check())
