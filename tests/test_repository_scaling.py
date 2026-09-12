from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from tgvio.domain.job_query import JobListFilter
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class SQLiteRepositoryScalingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _seed_to(self, total: int) -> None:
        conn = self.repo._require()
        cursor = await conn.execute("SELECT COUNT(*) FROM jobs")
        current = int((await cursor.fetchone())[0])
        await cursor.close()
        if current >= total:
            return
        async with self.repo._write_transaction() as write:
            for index in range(current, total):
                await write.execute(
                    """
                    INSERT INTO jobs(id, owner_id, destination, state, created_at, updated_at)
                    VALUES(?,?,?,?,datetime('2026-01-01', ?),datetime('2026-01-01', ?))
                    """,
                    (
                        f"scale-{index:04d}",
                        7 if index % 2 == 0 else 8,
                        "@scale",
                        "succeeded",
                        f"+{index} seconds",
                        f"+{index} seconds",
                    ),
                )

    async def test_recent_owner_query_is_bounded_at_100_and_1000_jobs(self) -> None:
        for total in (100, 1000):
            await self._seed_to(total)
            recent = await asyncio.wait_for(
                self.repo.list_recent(owner_id=7, limit=50),
                timeout=5.0,
            )
            self.assertEqual(len(recent), min(50, total // 2))
            self.assertTrue(all(job.owner_id == 7 for job in recent))

        conn = self.repo._require()
        cursor = await conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT id FROM jobs
            WHERE owner_id=?
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (7, 50),
        )
        plan = " ".join(str(row[3]) for row in await cursor.fetchall())
        await cursor.close()
        self.assertIn("idx_jobs_owner_state", plan)

    async def test_sql_job_page_remains_bounded_at_1000_jobs(self) -> None:
        await self._seed_to(1000)
        page = await asyncio.wait_for(
            self.repo.page_jobs(
                owner_id=7,
                filter=JobListFilter.COMPLETED,
                page=37,
                page_size=5,
            ),
            timeout=5.0,
        )
        self.assertEqual(page.total, 500)
        self.assertEqual(page.page, 37)
        self.assertEqual(len(page.entries), 5)
        self.assertTrue(all(entry.job.owner_id == 7 for entry in page.entries))
        self.assertTrue(all(entry.job.state.value == "succeeded" for entry in page.entries))

    async def test_read_pressure_remains_event_loop_cooperative(self) -> None:
        await self._seed_to(1000)
        stop = asyncio.Event()
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0)

        async def pressure() -> None:
            for _ in range(20):
                await self.repo.list_recent(owner_id=7, limit=50)
                await self.repo.get_stats_snapshot(owner_id=7)

        ticker_task = asyncio.create_task(ticker())
        try:
            await asyncio.wait_for(pressure(), timeout=10.0)
        finally:
            stop.set()
            await ticker_task

        self.assertGreater(ticks, 20)
