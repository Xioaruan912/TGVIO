from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.domain.job import Job, JobState
from tgvio.domain.job_query import JobListFilter
from tgvio.domain.maintenance import business_day_bounds
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class DurableJobQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _insert_job(
        self,
        job_id: str,
        *,
        owner_id: int = 42,
        state: str = "received",
        held: bool = False,
        error_code: str | None = None,
        policy: dict[str, object] | None = None,
        ordinal: int = 0,
    ) -> None:
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO jobs(
                    id, owner_id, destination, state, policy_json, error_code,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,datetime('2026-09-12', ?),datetime('2026-09-12', ?))
                """,
                (
                    job_id,
                    owner_id,
                    "@channel",
                    state,
                    json.dumps(policy or {}, separators=(",", ":")),
                    error_code,
                    f"+{ordinal} seconds",
                    f"+{ordinal} seconds",
                ),
            )
            await conn.execute(
                "INSERT INTO job_controls(job_id, hold_requested) VALUES(?,?)",
                (job_id, int(held)),
            )
            await conn.execute(
                "INSERT INTO job_schedule(job_id, accepted_at) VALUES(?,datetime('2026-09-12', ?))",
                (job_id, f"+{ordinal} seconds"),
            )

    async def _insert_failed_archive(
        self,
        job_id: str,
        *,
        error_code: str = "archive_object_transfer_failed",
    ) -> None:
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO archive_packages(
                    id, job_id, layout_version, remote_path, staging_path,
                    state, manifest_json, error_code
                ) VALUES(?,?,?,?,?,'failed','{}',?)
                """,
                (
                    f"arc_{job_id}",
                    job_id,
                    "v1",
                    f"archive/{job_id}",
                    f".staging/{job_id}",
                    error_code,
                ),
            )

    async def test_job_pages_filter_in_sql_and_preserve_owner_isolation(self) -> None:
        fixtures = (
            ("job-received", "received", False),
            ("job-held", "planned", True),
            ("job-publishing", "publishing", False),
            ("job-failed", "failed", False),
            ("job-succeeded", "succeeded", False),
            ("job-cancelled", "cancelled", False),
        )
        for ordinal, (job_id, state, held) in enumerate(fixtures):
            await self._insert_job(job_id, state=state, held=held, ordinal=ordinal)
        await self._insert_job(
            "foreign-failed",
            owner_id=7,
            state="failed",
            ordinal=99,
        )

        all_page = await self.repo.page_jobs(
            owner_id=42,
            filter=JobListFilter.ALL,
            page=0,
            page_size=3,
        )
        self.assertEqual(all_page.total, 6)
        self.assertEqual(len(all_page.entries), 3)
        self.assertTrue(all(entry.job.owner_id == 42 for entry in all_page.entries))
        self.assertTrue(all(entry.accepted_order is not None for entry in all_page.entries))

        active = await self.repo.page_jobs(owner_id=42, filter=JobListFilter.ACTIVE)
        self.assertEqual(
            {entry.job.id for entry in active.entries},
            {"job-received", "job-publishing"},
        )
        held = await self.repo.page_jobs(owner_id=42, filter=JobListFilter.HELD)
        self.assertEqual([entry.job.id for entry in held.entries], ["job-held"])
        self.assertTrue(held.entries[0].held)
        failed = await self.repo.page_jobs(owner_id=42, filter=JobListFilter.FAILED)
        self.assertEqual([entry.job.id for entry in failed.entries], ["job-failed"])
        completed = await self.repo.page_jobs(owner_id=42, filter=JobListFilter.COMPLETED)
        self.assertEqual(
            {entry.job.id for entry in completed.entries},
            {"job-succeeded", "job-cancelled"},
        )

        clamped = await self.repo.page_jobs(
            owner_id=42,
            filter=JobListFilter.ALL,
            page=999,
            page_size=3,
        )
        self.assertEqual(clamped.page, 1)
        self.assertEqual(len(clamped.entries), 3)

        held_order = held.entries[0].accepted_order
        assert held_order is not None
        resolved = await self.repo.get_by_accepted_order(42, held_order)
        foreign = await self.repo.get_by_accepted_order(7, held_order)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, "job-held")
        self.assertIsNone(foreign)

    async def test_failure_center_excludes_auto_recovery_pending_and_prioritizes_publish_review(self) -> None:
        recovery = {
            "auto_recovery": {
                "version": 1,
                "enabled": True,
                "max_attempts": 3,
                "base_delay_seconds": 15,
                "max_delay_seconds": 300,
            }
        }
        await self._insert_job(
            "publish-review",
            state="failed",
            error_code="publish_uncertain",
            policy={
                **recovery,
                "auto_recovery_job": {"status": "quarantined"},
            },
            ordinal=1,
        )
        await self._insert_job(
            "ordinary-failed",
            state="failed",
            error_code="media_analysis_failed",
            ordinal=2,
        )
        await self._insert_job(
            "retry-pending",
            state="failed",
            error_code="publish_failed",
            policy=recovery,
            ordinal=3,
        )
        await self._insert_job(
            "retry-exhausted",
            state="failed",
            error_code="publish_failed",
            policy={
                **recovery,
                "auto_recovery_job": {"status": "exhausted"},
            },
            ordinal=4,
        )
        await self._insert_job("archive-final", state="succeeded", ordinal=5)
        await self._insert_failed_archive("archive-final")
        await self._insert_job(
            "archive-pending",
            state="succeeded",
            policy=recovery,
            ordinal=6,
        )
        await self._insert_failed_archive("archive-pending")
        await self._insert_job(
            "archive-exhausted",
            state="succeeded",
            policy={
                **recovery,
                "auto_recovery_archive": {"status": "exhausted"},
            },
            ordinal=7,
        )
        await self._insert_failed_archive("archive-exhausted")
        await self._insert_job(
            "foreign-failure",
            owner_id=7,
            state="failed",
            error_code="publish_failed",
            ordinal=8,
        )

        page = await self.repo.page_failures(owner_id=42, page=0, page_size=10)
        self.assertEqual(page.total, 5)
        ids = [entry.job.id for entry in page.entries]
        self.assertEqual(ids[0], "publish-review")
        self.assertTrue(all(entry.accepted_order is not None for entry in page.entries))
        self.assertNotIn("retry-pending", ids)
        self.assertNotIn("archive-pending", ids)
        self.assertNotIn("foreign-failure", ids)
        self.assertIn("ordinary-failed", ids)
        self.assertIn("retry-exhausted", ids)
        self.assertIn("archive-final", ids)
        self.assertIn("archive-exhausted", ids)

        archive = next(entry for entry in page.entries if entry.job.id == "archive-final")
        self.assertFalse(archive.job_actionable)
        self.assertTrue(archive.archive_actionable)
        self.assertEqual(archive.archive_package_id, "arc_archive-final")

    async def test_held_filter_ignores_stale_hold_flag_on_terminal_job(self) -> None:
        await self._insert_job(
            "active-held",
            state="planned",
            held=True,
            ordinal=1,
        )
        await self._insert_job(
            "terminal-held",
            state="succeeded",
            held=True,
            ordinal=2,
        )

        held = await self.repo.page_jobs(owner_id=42, filter=JobListFilter.HELD)
        all_jobs = await self.repo.page_jobs(
            owner_id=42,
            filter=JobListFilter.ALL,
            page_size=10,
        )

        self.assertEqual([entry.job.id for entry in held.entries], ["active-held"])
        terminal = next(entry for entry in all_jobs.entries if entry.job.id == "terminal-held")
        self.assertFalse(terminal.held)


    async def test_history_filter_default_hiding_and_pending(self) -> None:
        await self._insert_job("j-received", state="received", ordinal=1)
        await self._insert_job("j-failed", state="failed", ordinal=2)
        await self._insert_job("j-succeeded", state="succeeded", ordinal=3)
        await self._insert_job("j-cancelled", state="cancelled", ordinal=4)
        await self._insert_job("j-hidden", state="succeeded", ordinal=5)
        await self._insert_job("j-hidden-failed", state="failed", ordinal=6)
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                "INSERT INTO job_visibility(job_id, hidden_at, reason) VALUES(?,?,?)",
                ("j-hidden", 1.0, "daily_rollover"),
            )
            await conn.execute(
                "INSERT INTO job_visibility(job_id, hidden_at, reason) VALUES(?,?,?)",
                ("j-hidden-failed", 1.0, "daily_rollover"),
            )

        all_page = await self.repo.page_jobs(owner_id=42, filter=JobListFilter.ALL, page_size=20)
        self.assertNotIn("j-hidden", {entry.job.id for entry in all_page.entries})
        self.assertNotIn("j-hidden-failed", {entry.job.id for entry in all_page.entries})

        history = await self.repo.page_jobs(
            owner_id=42, filter=JobListFilter.HISTORY, page_size=20
        )
        self.assertEqual(
            {entry.job.id for entry in history.entries},
            {"j-hidden", "j-hidden-failed"},
        )

        pending = await self.repo.page_jobs(
            owner_id=42, filter=JobListFilter.PENDING, page_size=20
        )
        self.assertEqual(
            {entry.job.id for entry in pending.entries},
            {"j-received", "j-failed"},
        )

        now = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc).timestamp()
        bounds = business_day_bounds(now, hour=6)
        today = await self.repo.page_jobs(
            owner_id=42,
            filter=JobListFilter.TODAY,
            page_size=20,
            business_day_start_epoch=bounds.start_epoch,
        )
        self.assertNotIn("j-hidden", {entry.job.id for entry in today.entries})
        self.assertIn("j-received", {entry.job.id for entry in today.entries})

    async def test_failure_center_excludes_hidden_jobs(self) -> None:
        await self._insert_job(
            "visible-failure", state="failed", error_code="media_analysis_failed", ordinal=1
        )
        await self._insert_job(
            "hidden-failure", state="failed", error_code="media_analysis_failed", ordinal=2
        )
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                "INSERT INTO job_visibility(job_id, hidden_at, reason) VALUES(?,?,?)",
                ("hidden-failure", 1.0, "daily_rollover"),
            )
        page = await self.repo.page_failures(owner_id=42, page=0, page_size=10)
        ids = {entry.job.id for entry in page.entries}
        self.assertIn("visible-failure", ids)
        self.assertNotIn("hidden-failure", ids)
        self.assertEqual(page.total, 1)


    async def test_display_numbers_are_per_business_day_and_sql_paged(self) -> None:
        for job_id in ("a", "b", "c"):
            await self.repo.create(
                Job(
                    owner_id=42,
                    destination="@channel",
                    items=[],
                    id=job_id,
                    state=JobState.RECEIVED,
                )
            )
        self.assertEqual(await self.repo.get_display_no("a"), 1)
        self.assertEqual(await self.repo.get_display_no("c"), 3)
        self.assertEqual(
            await self.repo.display_numbers_for(("a", "c")),
            {"a": 1, "c": 3},
        )

        day = business_day_bounds(time.time()).day
        resolved = await self.repo.get_by_display_number(42, day, 2)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, "b")
        # Old/other business days no longer resolve a bare number.
        self.assertIsNone(await self.repo.get_by_display_number(42, "1999-01-01", 1))

        page = await self.repo.page_jobs(owner_id=42, filter=JobListFilter.ALL, page_size=10)
        by_id = {entry.job.id: entry for entry in page.entries}
        self.assertEqual(by_id["a"].display_no, 1)
        self.assertEqual(by_id["a"].label_number, 1)

    async def test_missing_display_identity_falls_back_to_fifo_order(self) -> None:
        await self._insert_job("legacy", state="received", ordinal=1)
        page = await self.repo.page_jobs(owner_id=42, filter=JobListFilter.ALL, page_size=10)
        entry = next(item for item in page.entries if item.job.id == "legacy")
        self.assertIsNone(entry.display_no)
        self.assertIsNotNone(entry.label_number)


if __name__ == "__main__":
    unittest.main()
