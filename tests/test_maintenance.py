from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from tgvio.application.maintenance import (
    HistoryMaintenanceRuntime,
    HistoryMaintenanceService,
)
from tgvio.application.runtime_flags import RuntimeFlags
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class _FakeCache:
    def __init__(self) -> None:
        self.calls = 0
        self.seen: tuple[str, ...] = ()

    async def stats(self):
        return SimpleNamespace(removed_dirs=0)

    async def cleanup_candidates(self, *, force: bool = False):
        return ()

    async def cleanup(self, *, force: bool = False, job_ids=None):
        self.calls += 1
        self.seen = tuple(job_ids or ())
        return SimpleNamespace(removed_dirs=len(self.seen))


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


class HistoryMaintenanceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.cache = _FakeCache()
        self.cleaner = _FakeStatusCleaner()
        self.now = datetime.now(timezone.utc).timestamp() + 3 * 86400
        self.service = HistoryMaintenanceService(
            self.repo,
            cache_operator=self.cache,
            status_cleaner=self.cleaner,
            log_dir=Path(self.tmp.name),
            now=lambda: self.now,
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _seed_job(
        self,
        job_id: str,
        *,
        state: str = "succeeded",
        error_code: str | None = None,
        age_seconds: int = -3600,
        with_status: bool = False,
    ) -> None:
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO jobs(
                    id, owner_id, destination, state, policy_json, error_code,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,datetime('now', ?),datetime('now', ?))
                """,
                (
                    job_id,
                    7,
                    "@channel",
                    state,
                    "{}",
                    error_code,
                    f"{age_seconds} seconds",
                    f"{age_seconds} seconds",
                ),
            )
        if with_status:
            await self.repo.save_job_display_message(
                SimpleNamespace(
                    job_id=job_id, chat_id=555, message_id=999, replacement_count=0
                )
            )

    async def test_safe_hide_preserves_history_and_clears_owned_artifacts(self) -> None:
        await self._seed_job("done", with_status=True)
        result = await self.service.run()
        self.assertEqual(result["hidden"], 1)
        self.assertEqual(self.cache.calls, 1)
        self.assertEqual(self.cache.seen, ("done",))
        self.assertEqual(self.cleaner.deleted, [(555, 999)])
        self.assertTrue(await self.repo.is_job_hidden("done"))
        # The Job row and its history are preserved, only default visibility changes.
        self.assertIsNotNone(await self.repo.get("done"))

    async def test_partial_and_uncertain_failures_are_never_hidden(self) -> None:
        await self._seed_job("partial", state="failed", error_code="publish_partial")
        await self._seed_job("uncertain", state="failed", error_code="publish_uncertain")
        result = await self.service.run()
        self.assertEqual(result["hidden"], 0)
        self.assertFalse(await self.repo.is_job_hidden("partial"))
        self.assertFalse(await self.repo.is_job_hidden("uncertain"))

    async def test_active_claim_blocks_hiding(self) -> None:
        await self._seed_job("claimed")
        now = int(datetime.now(timezone.utc).timestamp())
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO job_phase_claims(
                    job_id, phase, holder_id, generation, claimed_at,
                    heartbeat_at, expires_at
                ) VALUES(?, 'download', 'holder', 1, ?, ?, ?)
                """,
                ("claimed", now, now, self.now + 600),
            )
        result = await self.service.run()
        self.assertEqual(result["hidden"], 0)
        self.assertFalse(await self.repo.is_job_hidden("claimed"))

    async def test_non_terminal_jobs_are_not_hidden(self) -> None:
        await self._seed_job("running", state="publishing")
        result = await self.service.run()
        self.assertEqual(result["hidden"], 0)
        self.assertFalse(await self.repo.is_job_hidden("running"))

    async def test_run_is_idempotent_per_business_day(self) -> None:
        await self._seed_job("done")
        first = await self.service.run()
        self.assertEqual(first["hidden"], 1)
        second = await self.service.run()
        self.assertTrue(second.get("skipped"))
        self.assertEqual(self.cache.calls, 1)

    async def test_recycles_expired_tokens_and_settled_outbox(self) -> None:
        await self._seed_job("done")
        now = int(self.now)
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO operation_tokens(
                    token, owner_id, action, resource_type, resource_id,
                    expected_revision, payload_hash, payload_json, expires_at
                ) VALUES('old-token',7,'undo','job','done',1,'h','{}',?)
                """,
                (now - 7200,),
            )
            await conn.execute(
                """
                INSERT INTO operation_tokens(
                    token, owner_id, action, resource_type, resource_id,
                    expected_revision, payload_hash, payload_json, expires_at
                ) VALUES('fresh-token',7,'undo','job','done',1,'h','{}',?)
                """,
                (now + 7200,),
            )
            await conn.execute(
                """
                INSERT INTO notification_outbox(
                    event_type, dedupe_key, payload_json, state, max_attempts,
                    next_attempt_at, created_at, updated_at
                ) VALUES('event','k','{}','sent',3,?,?,?)
                """,
                (now, now - 90000, now - 90000),
            )
        result = await self.service.run()
        self.assertEqual(result["tokens_recycled"], 1)
        self.assertEqual(result["outbox_pruned"], 1)
        conn = self.repo._require()
        cursor = await conn.execute("SELECT token FROM operation_tokens")
        rows = await cursor.fetchall()
        await cursor.close()
        self.assertEqual([row["token"] for row in rows], ["fresh-token"])

    async def test_runtime_respects_flag_and_owner_runs(self) -> None:
        calls: list[int] = []

        class _Service:
            def __init__(self) -> None:
                self.hour = 6

            def set_hour(self, hour: int) -> None:
                self.hour = hour

            async def run(self):
                calls.append(1)
                return {}

        flags = RuntimeFlags()
        runtime = HistoryMaintenanceRuntime(_Service(), flags)  # type: ignore[arg-type]
        await flags.set(self.repo, "daily_cleanup_enabled", "false")
        await runtime._tick()
        self.assertEqual(calls, [])
        await flags.set(self.repo, "daily_cleanup_enabled", "true")
        await runtime._tick()
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
