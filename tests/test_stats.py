import asyncio
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.repository import SQLiteRepository
from src.services.stats import StatsService
from src.services.stats import RuntimeStatsSnapshot
from src.views.stats import stats_view


class StatsRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.download_root = root / "downloads"
        self.download_root.mkdir()
        self.repo = SQLiteRepository(root / "state.sqlite3", download_root=self.download_root)
        await self.repo.open()
        await self.repo.migrate()

    async def asyncTearDown(self) -> None:
        await self.repo.close()

    async def test_stats_are_idempotent_across_reconcile(self) -> None:
        media = self.download_root / "job-1" / "video.mp4"
        media.parent.mkdir()
        media.write_bytes(b"x" * 128)

        success = await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/video",
            legacy_seq=1,
        )
        claim = await self.repo.claim_next_download(owner="test-download")
        self.assertIsNotNone(claim)
        ready = await self.repo.record_download_ready(
            success.id,
            [str(media)],
            expected_revision=claim.job.revision,
        )
        self.assertTrue(ready.applied)
        publish_claim = await self.repo.claim_next_publish(owner="test-publish")
        self.assertIsNotNone(publish_claim)
        published = await self.repo.record_published_messages(
            success.id,
            [(-1001, 99, "channel")],
            expected_revision=publish_claim.job.revision,
        )
        self.assertTrue(published.applied)

        attempt = await self.repo.create_backup_attempt(
            job_id=success.id,
            state="running",
            remote_dir="backup",
        )
        backup_file = await self.repo.create_backup_file(
            attempt_id=attempt.id,
            local_path=str(media),
            remote_name="video.mp4",
            size_bytes=128,
        )
        await self.repo.update_backup_file_status(
            backup_file.id,
            state="succeeded",
            bytes_done=128,
        )
        # Repeating the same terminal status must not count the file twice.
        await self.repo.update_backup_file_status(
            backup_file.id,
            state="succeeded",
            bytes_done=128,
        )

        failed = await self.repo.accept_job(
            kind="url", user_id=1, state="downloading", source_kind="url", legacy_seq=2
        )
        failed_result = await self.repo.transition_job(
            failed.id,
            expected_revision=failed.revision,
            to_state="failed",
            event_type="failed",
            payload={"schema_version": 1, "error_code": "network_timeout"},
        )
        self.assertTrue(failed_result.applied)

        cancelled = await self.repo.accept_job(
            kind="url", user_id=1, state="queued", source_kind="url", legacy_seq=3
        )
        cancelled_result = await self.repo.transition_job(
            cancelled.id,
            expected_revision=cancelled.revision,
            to_state="cancelled",
            event_type="cancelled",
            payload={"schema_version": 1},
        )
        self.assertTrue(cancelled_result.applied)

        before = await self.repo.stats_snapshot()
        self.assertEqual(before["totals"]["accepted_jobs"], 3)
        self.assertEqual(before["totals"]["succeeded_jobs"], 1)
        self.assertEqual(before["totals"]["failed_jobs"], 1)
        self.assertEqual(before["totals"]["cancelled_jobs"], 1)
        self.assertEqual(before["totals"]["downloaded_bytes"], 128)
        self.assertEqual(before["totals"]["published_bytes"], 128)
        self.assertEqual(before["totals"]["backed_up_bytes"], 128)

        first = await self.repo.reconcile_daily_stats()
        second = await self.repo.reconcile_daily_stats()
        self.assertGreaterEqual(first, 0)
        self.assertEqual(second, 0)
        after = await self.repo.stats_snapshot()
        self.assertEqual(after["totals"], before["totals"])

    async def test_same_metric_scope_can_be_counted_on_different_utc_days(self) -> None:
        conn = self.repo._require_conn()
        async with self.repo._write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            first = await self.repo._apply_stat_metric_tx(
                conn,
                scope_key="backup_file:7",
                metric="backed_up_bytes",
                value=10,
                timestamp=0.0,
            )
            duplicate = await self.repo._apply_stat_metric_tx(
                conn,
                scope_key="backup_file:7",
                metric="backed_up_bytes",
                value=10,
                timestamp=0.0,
            )
            second_day = await self.repo._apply_stat_metric_tx(
                conn,
                scope_key="backup_file:7",
                metric="backed_up_bytes",
                value=10,
                timestamp=86400.0,
            )
            await conn.commit()
        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertTrue(second_day)

    async def test_reconcile_one_hundred_jobs_one_thousand_items_stays_cooperative(self) -> None:
        for seq in range(1, 101):
            job = await self.repo.accept_job(
                kind="album",
                user_id=1,
                state="queued",
                source_kind="telegram",
                legacy_seq=1000 + seq,
                items=[{"media_kind": "video", "size_bytes": 1024} for _ in range(10)],
            )
            conn = self.repo._require_conn()
            await conn.execute(
                """UPDATE jobs SET state='succeeded',download_state='succeeded',
                          publish_state='succeeded',finished_at=updated_at
                   WHERE id=?""",
                (job.id,),
            )
            await conn.commit()
        conn = self.repo._require_conn()
        await conn.execute("DELETE FROM daily_stats")
        await conn.execute("DELETE FROM stat_metric_applied")
        await conn.commit()

        ticks = 0
        stop = False

        async def ticker() -> None:
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0)

        tick_task = asyncio.create_task(ticker())
        try:
            await asyncio.wait_for(self.repo.reconcile_daily_stats(), timeout=3.0)
        finally:
            stop = True
            await tick_task

        stats = await self.repo.stats_snapshot()
        self.assertEqual(stats["totals"]["accepted_jobs"], 100)
        self.assertEqual(stats["totals"]["succeeded_jobs"], 100)
        self.assertEqual(stats["totals"]["downloaded_bytes"], 1000 * 1024)
        self.assertEqual(stats["totals"]["published_bytes"], 1000 * 1024)
        self.assertGreater(ticks, 10)


class StatsViewTests(unittest.TestCase):
    def test_stats_view_renders_safe_summary_only(self) -> None:
        state = RuntimeStatsSnapshot(
            uptime_seconds=90061,
            today_accepted=4,
            today_succeeded=3,
            today_failed=1,
            today_cancelled=0,
            total_published_bytes=2 * 1024**3,
            total_backed_up_bytes=1024**3,
            saved_upload_bytes=0,
            running=1,
            waiting=2,
            failed=1,
            cpu_percent=12.5,
            memory_mb=256.0,
            disk_used_bytes=10 * 1024**3,
            disk_total_bytes=50 * 1024**3,
            disk_free_bytes=40 * 1024**3,
            disk_reserved_bytes=2 * 1024**3,
            protected_cache_bytes=1024**3,
            reclaimable_cache_bytes=512 * 1024**2,
            disk_enforce=True,
            disk_healthy=True,
            telegram_connected=True,
            webdav_enabled=True,
            webdav_health="正常",
            webdav_age_seconds=180,
            database_ok=True,
            recent_errors=(("network_timeout", 2),),
            recent_events=(("published", 3),),
        )
        text, buttons = stats_view(state)
        self.assertIn("📊 运行状态", text)
        self.assertIn("累计发布：2.0 GB", text)
        self.assertIn("WebDAV：正常（3 分钟前）", text)
        self.assertIn("network_timeout×2", text)
        self.assertIn("published×3", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("/app/", text)
        callbacks = [button.data.decode() for row in buttons for button in row]
        self.assertEqual(callbacks, ["h:status", "h:health", "h:diag", "h:r"])


class _DiagClient:
    def is_connected(self) -> bool:
        return True


class _DiagBackup:
    def config_snapshot(self):
        return {"enabled": True, "url": "https://user:secret@example.invalid/dav"}

    def logs_snapshot(self):
        return {}


class _DiagPipeline:
    def __init__(self) -> None:
        self.client = _DiagClient()
        self.disk = None
        self.job_queue = None
        self.proxy_cfg = {
            "auto": True,
            "proxies": ["http://user:secret@example.invalid:8080"],
        }


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_never_emit_credentials_or_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            downloads.mkdir()
            repo = SQLiteRepository(root / "state.sqlite3", download_root=downloads)
            await repo.open()
            await repo.migrate()
            try:
                service = StatsService(_DiagPipeline(), repo, _DiagBackup())
                with patch.dict(
                    os.environ,
                    {"APP_COMMIT": "deadbeef"},
                    clear=False,
                ):
                    text = await service.diagnostics_text()
            finally:
                await repo.close()
        self.assertIn("commit=deadbeef", text)
        self.assertIn("proxy_count=1", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("example.invalid", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)

    async def test_event_summary_contains_types_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            downloads.mkdir()
            repo = SQLiteRepository(root / "state.sqlite3", download_root=downloads)
            await repo.open()
            await repo.migrate()
            try:
                job = await repo.accept_job(
                    kind="url", user_id=1, state="queued", source_kind="url",
                    source_url="https://secret.invalid/private",
                    event_payload={"schema_version": 1, "caption": "private text"},
                )
                await repo.record_job_event(
                    job.id, "diagnostic_test",
                    payload={"schema_version": 1, "secret": "do-not-export"},
                )
                events = await repo.stats_event_summary()
            finally:
                await repo.close()
        rendered = repr(events)
        self.assertIn("diagnostic_test", rendered)
        self.assertNotIn("private text", rendered)
        self.assertNotIn("do-not-export", rendered)

    async def test_health_text_is_local_only_and_reports_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            downloads.mkdir()
            heartbeat = root / "runtime-health.json"
            heartbeat.write_text(
                json.dumps({"heartbeat_at": __import__("time").time(), "ready": True, "pid": os.getpid()}),
                encoding="utf-8",
            )
            repo = SQLiteRepository(root / "state.sqlite3", download_root=downloads)
            await repo.open()
            await repo.migrate()
            try:
                service = StatsService(_DiagPipeline(), repo, _DiagBackup())
                with patch.dict(
                    os.environ,
                    {"HEALTH_HEARTBEAT_FILE": str(heartbeat)},
                    clear=False,
                ):
                    text = await service.health_text()
            finally:
                await repo.close()
        self.assertIn("Liveness：正常", text)
        self.assertIn("Readiness：就绪", text)
        self.assertIn("仅连接状态", text)
        self.assertIn("仅缓存状态", text)
        self.assertNotIn("example.invalid", text)
