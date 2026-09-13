from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from tgvio.application.maintenance import (
    DailyMaintenanceRuntime,
    DailyMaintenanceService,
)
from tgvio.application.runtime_flags import RuntimeFlags
from tgvio.domain.job import Job, JobState
from tgvio.infrastructure.sqlite import SQLiteJobRepository


def _job(job_id: str, state: JobState = JobState.RECEIVED) -> Job:
    return Job(owner_id=7, destination="@channel", items=[], id=job_id, state=state)


class _FakeCache:
    def __init__(self) -> None:
        self.calls = 0

    async def stats(self):
        return SimpleNamespace(removed_dirs=0)

    async def cleanup_candidates(self, *, force: bool = False):
        return ()

    async def cleanup(self, *, force: bool = False, job_ids=None):
        self.calls += 1
        return SimpleNamespace(removed_dirs=3)


class _FakeStatusCleaner:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, peer_id: int, message_id: int) -> None:
        self.deleted.append((peer_id, message_id))


class MaintenanceRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_runtime_flags_persist(self) -> None:
        flags = RuntimeFlags()
        self.assertTrue(flags.bool("alerts_enabled", True))
        await flags.set(self.repo, "alerts_enabled", "false")
        reloaded = RuntimeFlags()
        await reloaded.load(self.repo)
        self.assertFalse(reloaded.bool("alerts_enabled", True))

    async def test_archive_day_seq_increments(self) -> None:
        self.assertEqual(await self.repo.next_archive_day_seq("2026-09-13"), 1)
        self.assertEqual(await self.repo.next_archive_day_seq("2026-09-13"), 2)
        self.assertEqual(await self.repo.next_archive_day_seq("2026-09-14"), 1)

    async def test_purge_clears_terminal_jobs_and_resets_numbering(self) -> None:
        await self.repo.create(_job("a"))
        await self.repo.transition("a", JobState.DOWNLOADING, event_type="x")
        await self.repo.transition("a", JobState.DOWNLOADED, event_type="x")
        await self.repo.transition("a", JobState.ANALYZING, event_type="x")
        await self.repo.transition("a", JobState.ANALYZED, event_type="x")
        await self.repo.transition("a", JobState.PLANNED, event_type="x")
        await self.repo.transition("a", JobState.PUBLISHING, event_type="x")
        await self.repo.transition("a", JobState.SUCCEEDED, event_type="x")
        await self.repo.create(_job("b"))

        result = await self.repo.purge_terminal_history(reset_numbering=True)
        self.assertEqual(result["jobs_deleted"], 1)
        self.assertEqual(result["remaining_jobs"], 1)
        # 'b' still runs, so numbering is not reset yet.
        await self.repo.transition("b", JobState.CANCELLED, event_type="x")
        result = await self.repo.purge_terminal_history(reset_numbering=True)
        self.assertEqual(result["jobs_deleted"], 1)
        self.assertEqual(result["remaining_jobs"], 0)
        await self.repo.create(_job("c"))
        self.assertEqual(await self.repo.get_accepted_order("c"), 1)


class DailyMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.cache = _FakeCache()
        self.cleaner = _FakeStatusCleaner()
        self.service = DailyMaintenanceService(
            self.repo,
            cache_operator=self.cache,
            status_cleaner=self.cleaner,
            log_dir=Path(self.tmp.name),
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_service_clears_cache_and_records(self) -> None:
        job = _job("j1")
        await self.repo.create(job)
        await self.repo.save_job_display_message(
            SimpleNamespace(job_id="j1", chat_id=555, message_id=999, replacement_count=0)
        )
        result = await self.service.run()
        self.assertEqual(self.cache.calls, 1)
        self.assertEqual(self.cleaner.deleted, [(555, 999)])
        self.assertGreaterEqual(result["jobs_deleted"], 0)

    async def test_runtime_runs_once_per_day_at_six(self) -> None:
        flags = RuntimeFlags()
        calls: list[int] = []
        original_run = self.service.run

        async def counting_run():
            calls.append(1)
            return await original_run()

        self.service.run = counting_run  # type: ignore[assignment]
        times = [datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc).timestamp()]  # 04:00 Beijing
        runtime = DailyMaintenanceRuntime(self.service, flags, now=lambda: times[0])
        await runtime._maybe_run()
        self.assertEqual(calls, [])
        times[0] = datetime(2026, 9, 13, 22, 30, tzinfo=timezone.utc).timestamp()  # 06:30 Beijing
        await runtime._maybe_run()
        self.assertEqual(len(calls), 1)
        await runtime._maybe_run()
        self.assertEqual(len(calls), 1)
        await runtime.stop()
