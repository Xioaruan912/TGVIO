import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.repository import MigrationChecksumError, MigrationError, SQLiteRepository
from src.services import BackupManager
from src.state_machine import InvalidTransition


class SQLiteRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.db_path = root / "state.sqlite3"
        self.backup_dir = root / "backups"
        self.download_root = root / "downloads"
        self.download_root.mkdir()
        self.repo = SQLiteRepository(
            self.db_path,
            backup_dir=self.backup_dir,
            download_root=self.download_root,
        )
        await self.repo.open()
        await self.repo.migrate()

    async def asyncTearDown(self) -> None:
        await self.repo.close()

    async def test_initial_migration_is_idempotent_and_pragmas_are_enforced(self) -> None:
        self.assertEqual(await self.repo.schema_versions(), [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(await self.repo.migrate(), [])
        check = await self.repo.self_check()
        self.assertEqual(check["integrity"], "ok")
        self.assertEqual(check["foreign_keys"], 1)
        self.assertGreaterEqual(check["busy_timeout"], 5000)
        self.assertEqual(check["synchronous"], 2)
        self.assertTrue(SQLiteRepository.REQUIRED_TABLES.issubset(check["tables"]))

    async def test_media_compat_metadata_merges_without_losing_existing_item_metadata(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="queued",
            source_kind="url",
            legacy_seq=88,
            source_url="https://example.invalid/media",
            items=[{"metadata": {"schema_version": 1, "existing": "keep"}}],
            event_payload={"schema_version": 1},
        )
        changed = await self.repo.set_job_item_media_metadata(
            88,
            [
                {
                    "container": "mov,mp4,m4a,3gp,3g2,mj2",
                    "video_codec": "h264",
                    "audio_codec": "aac",
                    "faststart": True,
                    "streaming_ready": True,
                }
            ],
        )
        self.assertEqual(changed, 1)
        items = await self.repo.list_job_items(job.id)
        payload = __import__("json").loads(items[0].metadata_json)
        self.assertEqual(payload["existing"], "keep")
        self.assertTrue(payload["media_compat"]["streaming_ready"])

    async def test_backup_attempt_pages_are_sql_backed_and_include_file_aggregates(self) -> None:
        job = await self.repo.accept_job(
            kind="url", user_id=1, state="queued", source_kind="url",
            source_url="https://example.invalid/a", legacy_seq=77,
            event_payload={"schema_version": 1},
        )
        attempt = await self.repo.create_backup_attempt(
            job_id=job.id, state="failed", remote_dir="backup/77"
        )
        job_dir = self.download_root / "job-77"
        job_dir.mkdir()
        one = job_dir / "one.mp4"; one.write_bytes(b"aaa")
        two = job_dir / "two.mp4"; two.write_bytes(b"bbbbb")
        first = await self.repo.create_backup_file(
            attempt_id=attempt.id, local_path=str(one), remote_name="one.mp4",
            size_bytes=3, state="succeeded",
        )
        second = await self.repo.create_backup_file(
            attempt_id=attempt.id, local_path=str(two), remote_name="two.mp4",
            size_bytes=5, state="failed",
        )
        await self.repo.update_backup_file_status(
            second.id, state="failed", error_code="webdav_server", error_message="safe"
        )

        page = await self.repo.list_backup_attempt_page(limit=5, offset=0)
        detail = await self.repo.backup_attempt_detail(attempt.id)
        files = await self.repo.list_backup_file_page(attempt.id, limit=5, offset=0)
        retry_ids = await self.repo.backup_retry_file_ids(attempt.id)

        self.assertEqual(page[0]["legacy_seq"], 77)
        self.assertEqual(page[0]["total_files"], 2)
        self.assertEqual(page[0]["total_bytes"], 8)
        self.assertEqual(page[0]["failed_files"], 1)
        self.assertEqual(detail["remote_dir"], "backup/77")
        self.assertEqual([item["id"] for item in files], [first.id, second.id])
        self.assertEqual(retry_ids, [second.id])

    async def test_durable_backup_autoretry_excludes_cache_missing(self) -> None:
        job = await self.repo.accept_job(
            kind="url", user_id=1, state="queued", source_kind="url",
            source_url="https://example.invalid/retry", legacy_seq=88,
            event_payload={"schema_version": 1},
        )
        attempt = await self.repo.create_backup_attempt(job_id=job.id, state="failed", remote_dir="backup/88")
        job_dir = self.download_root / "job-88"; job_dir.mkdir()
        a = job_dir / "a.mp4"; a.write_bytes(b"a")
        b = job_dir / "b.mp4"; b.write_bytes(b"b")
        first = await self.repo.create_backup_file(
            attempt_id=attempt.id, local_path=str(a), remote_name="a.mp4", size_bytes=1, state="failed"
        )
        second = await self.repo.create_backup_file(
            attempt_id=attempt.id, local_path=str(b), remote_name="b.mp4", size_bytes=1, state="failed"
        )
        await self.repo.update_backup_file_status(first.id, state="failed", error_code="webdav_server")
        await self.repo.update_backup_file_status(second.id, state="failed", error_code="cache_missing")
        due = await self.repo.due_backup_retry_file_ids(now=9999999999)
        self.assertIn(first.id, due)
        self.assertNotIn(second.id, due)

    async def test_backup_manager_remote_delete_uses_only_durable_file_records(self) -> None:
        job = await self.repo.accept_job(
            kind="url", user_id=1, state="queued", source_kind="url",
            source_url="https://example.invalid/delete", legacy_seq=89,
            event_payload={"schema_version": 1},
        )
        attempt = await self.repo.create_backup_attempt(job_id=job.id, state="succeeded", remote_dir="backup/89")
        job_dir = self.download_root / "job-89"; job_dir.mkdir()
        one = job_dir / "one.mp4"; one.write_bytes(b"a")
        two = job_dir / "two.mp4"; two.write_bytes(b"b")
        first = await self.repo.create_backup_file(
            attempt_id=attempt.id, local_path=str(one), remote_name="hash-one.mp4", size_bytes=1, state="succeeded"
        )
        second = await self.repo.create_backup_file(
            attempt_id=attempt.id, local_path=str(two), remote_name="hash-two.mp4", size_bytes=1, state="succeeded"
        )
        pipeline = SimpleNamespace(
            repository=self.repo,
            webdav_cfg={"url": "https://dav.invalid", "user": "u", "pass": "p"},
        )
        manager = BackupManager(pipeline)
        calls = []

        def fake_delete(base_url, remote_dir, filename, user, passwd, retries=2):
            calls.append((remote_dir, filename, retries))
            return True

        with patch("src.services.backup_manager.webdav.delete_remote", side_effect=fake_delete):
            result = await manager.delete_attempt(attempt.id)

        self.assertIn("已删除远端文件 2 个", result)
        self.assertEqual(calls, [("backup/89", "hash-one.mp4", 0), ("backup/89", "hash-two.mp4", 0)])
        files = await self.repo.list_backup_file_page(attempt.id, limit=5, offset=0)
        self.assertEqual({item["state"] for item in files}, {"deleted"})
        detail = await self.repo.backup_attempt_detail(attempt.id)
        self.assertEqual(detail["state"], "deleted")

    async def test_cleanup_inventory_exposes_only_durable_cleanup_facts(self) -> None:
        job_dir = self.download_root / "job-77"
        job_dir.mkdir()
        local_path = job_dir / "cached.mp4"
        local_path.write_bytes(b"cache")
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="failed",
            source_kind="url",
            legacy_seq=77,
            items=[{"local_path": str(local_path), "size_bytes": 5, "metadata": {"schema_version": 1}}],
            event_payload={"schema_version": 1},
            now=100.0,
        )
        conn = self.repo._require_conn()
        await conn.execute(
            "UPDATE jobs SET local_dir=?,finished_at=?,next_retry_at=? WHERE id=?",
            (str(job_dir), 200.0, 500.0, job.id),
        )
        await conn.commit()
        attempt = await self.repo.create_backup_attempt(
            job_id=job.id,
            state="failed",
            remote_dir="remote",
            now=210.0,
        )
        self.assertGreater(attempt.id, 0)

        rows = await self.repo.cleanup_inventory()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["job_id"], job.id)
        self.assertEqual(row["legacy_seq"], 77)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["local_dir"], str(job_dir))
        self.assertEqual(row["item_bytes"], 5)
        self.assertEqual(row["next_retry_at"], 500.0)
        self.assertEqual(row["backup_state"], "failed")

    async def test_disk_cleanup_claim_and_finalize_are_revision_guarded(self) -> None:
        job_dir = self.download_root / "job-78"
        job_dir.mkdir()
        local_path = job_dir / "cached.mp4"
        local_path.write_bytes(b"cache")
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="succeeded",
            source_kind="url",
            legacy_seq=78,
            items=[{"local_path": str(local_path), "size_bytes": 5, "metadata": {"schema_version": 1}}],
            event_payload={"schema_version": 1},
            now=100.0,
        )
        conn = self.repo._require_conn()
        await conn.execute(
            "UPDATE jobs SET local_dir=?,finished_at=? WHERE id=?",
            (str(job_dir), 200.0, job.id),
        )
        await conn.commit()
        current = await self.repo.get_job(job.id)
        claim = await self.repo.acquire_disk_cleanup_claim(
            job.id,
            expected_revision=current.revision,
            owner="test-cleaner",
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim.revision, current.revision + 1)
        stale = await self.repo.finish_disk_cleanup(
            job.id,
            expected_revision=current.revision,
            owner="test-cleaner",
            freed_bytes=5,
        )
        self.assertFalse(stale)
        applied = await self.repo.finish_disk_cleanup(
            job.id,
            expected_revision=claim.revision,
            owner="test-cleaner",
            freed_bytes=5,
        )
        self.assertTrue(applied)
        self.assertEqual(await self.repo.cleanup_inventory(), [])
        items = await self.repo.list_job_items(job.id)
        self.assertIsNone(items[0].local_path)
        events = await self.repo.list_job_events(job.id)
        self.assertEqual(events[-1].event_type, "disk_cleanup")

    async def test_abort_disk_cleanup_releases_claim_without_clearing_paths(self) -> None:
        job_dir = self.download_root / "job-79"
        job_dir.mkdir()
        local_path = job_dir / "cached.mp4"
        local_path.write_bytes(b"cache")
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="failed",
            source_kind="url",
            legacy_seq=79,
            items=[{"local_path": str(local_path), "size_bytes": 5, "metadata": {"schema_version": 1}}],
            event_payload={"schema_version": 1},
        )
        conn = self.repo._require_conn()
        await conn.execute("UPDATE jobs SET local_dir=? WHERE id=?", (str(job_dir), job.id))
        await conn.commit()
        current = await self.repo.get_job(job.id)
        claim = await self.repo.acquire_disk_cleanup_claim(
            job.id, expected_revision=current.revision, owner="test-cleaner"
        )
        self.assertIsNotNone(claim)
        self.assertTrue(
            await self.repo.abort_disk_cleanup_claim(
                job.id,
                expected_revision=claim.revision,
                owner="test-cleaner",
                freed_bytes=2,
            )
        )
        items = await self.repo.list_job_items(job.id)
        self.assertEqual(items[0].local_path, str(local_path))
        inventory = await self.repo.cleanup_inventory()
        self.assertEqual(inventory[0]["local_dir"], str(job_dir))
        events = await self.repo.list_job_events(job.id)
        self.assertEqual(events[-1].event_type, "disk_cleanup_failed")

    async def test_restart_releases_interrupted_disk_cleanup_claim(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="succeeded",
            source_kind="url",
            event_payload={"schema_version": 1},
        )
        claim = await self.repo.acquire_disk_cleanup_claim(
            job.id,
            expected_revision=job.revision,
            owner="dead-process",
        )
        self.assertIsNotNone(claim)
        self.assertEqual(await self.repo.release_interrupted_disk_cleanup_claims(), 1)
        conn = self.repo._require_conn()
        cursor = await conn.execute(
            "SELECT claim_owner,claim_kind FROM jobs WHERE id=?",
            (job.id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        self.assertIsNone(row["claim_owner"])
        self.assertIsNone(row["claim_kind"])
        events = await self.repo.list_job_events(job.id)
        self.assertEqual(events[-1].event_type, "disk_cleanup_interrupted")

    async def test_runtime_entities_preserve_item_and_text_order(self) -> None:
        job = await self.repo.accept_job(
            kind="collection",
            user_id=42,
            state="queued",
            source_kind="telegram",
            items=[
                {"source_chat_id": 9, "source_message_id": 101, "grouped_id": 7, "media_kind": "photo", "metadata": {"schema_version": 1}},
                {"source_chat_id": 9, "source_message_id": 102, "grouped_id": 7, "media_kind": "document", "metadata": {"schema_version": 1}},
            ],
            texts=["first", "second"],
            event_payload={"schema_version": 1, "legacy_seq": 500},
            now=130.0,
        )
        items = await self.repo.list_job_items(job.id)
        self.assertEqual([item.ordinal for item in items], [1, 2])
        self.assertEqual([item.source_message_id for item in items], [101, 102])
        self.assertEqual(await self.repo.list_job_texts(job.id), ["first", "second"])

        for target, event in (
            ("downloading", "download_started"),
            ("ready", "download_completed"),
            ("publishing", "publish_started"),
        ):
            current = await self.repo.get_job(job.id)
            result = await self.repo.transition_job(
                job.id,
                expected_revision=current.revision,
                to_state=target,
                event_type=event,
                payload={"schema_version": 1},
            )
            self.assertTrue(result.applied)
        current = await self.repo.get_job(job.id)
        await self.repo.record_published_messages(
            job.id,
            [(-1001, 11, "channel"), (-1002, 12, "comment")],
            expected_revision=current.revision,
        )
        refs = await self.repo.list_published_messages(job.id)
        self.assertEqual([(ref.peer_id, ref.message_id) for ref in refs], [(-1001, 11), (-1002, 12)])
        self.assertEqual((await self.repo.get_job(job.id)).state, "succeeded")

    async def test_revision_cas_rejects_stale_transition_without_duplicate_event(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/a",
            event_payload={"schema_version": 1},
        )
        first = await self.repo.transition_job(
            job.id,
            expected_revision=job.revision,
            to_state="downloading",
            event_type="download_started",
            payload={"schema_version": 1},
        )
        self.assertTrue(first.applied)
        stale = await self.repo.transition_job(
            job.id,
            expected_revision=job.revision,
            to_state="cancelled",
            event_type="cancelled",
            payload={"schema_version": 1},
        )
        self.assertFalse(stale.applied)
        self.assertEqual(stale.job.state, "downloading")
        events = await self.repo.list_job_events(job.id)
        self.assertEqual([event.event_type for event in events], ["accepted", "download_started"])

    async def test_retry_metadata_and_terminal_error_are_persisted_safely(self) -> None:
        job = await self.repo.accept_job(
            kind="url", user_id=1, state="downloading", source_kind="url",
            event_payload={"schema_version": 1},
        )
        await self.repo.record_job_retry(
            job.id,
            phase="download",
            error_code="network_timeout",
            error_message="https://user:secret@example.invalid token=secret",
            retry_count=1,
            next_retry_at=150.0,
        )
        detail = await self.repo.job_detail(job.id, user_id=1)
        self.assertEqual(detail["error_code"], "network_timeout")
        self.assertEqual(detail["retry_count"], 1)
        self.assertEqual(detail["next_retry_at"], 150.0)
        self.assertNotIn("secret", detail["error_message"])
        events = await self.repo.list_job_events(job.id)
        self.assertEqual(events[-1].event_type, "download_retry_scheduled")

        current = await self.repo.get_job(job.id)
        result = await self.repo.transition_job(
            job.id,
            expected_revision=current.revision,
            to_state="failed",
            event_type="failed",
            payload={
                "schema_version": 1,
                "error_code": "network_timeout",
                "error_message": "safe summary",
                "retry_count": 2,
            },
        )
        self.assertTrue(result.applied)
        detail = await self.repo.job_detail(job.id, user_id=1)
        self.assertEqual(detail["retry_count"], 2)
        self.assertIsNone(detail["next_retry_at"])

    async def test_terminal_transition_is_rejected(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="cancelled",
            source_kind="url",
            event_payload={"schema_version": 1},
        )
        with self.assertRaises(InvalidTransition):
            await self.repo.transition_job(
                job.id,
                expected_revision=job.revision,
                to_state="queued",
                event_type="retry",
                payload={"schema_version": 1},
            )

    async def test_concurrent_download_claim_has_single_winner(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="queued",
            source_kind="url",
            event_payload={"schema_version": 1},
        )
        second = SQLiteRepository(self.db_path, download_root=self.download_root)
        await second.open()
        try:
            claims = await asyncio.gather(
                self.repo.claim_next_download("worker-a"),
                second.claim_next_download("worker-b"),
            )
        finally:
            await second.close()
        winners = [claim for claim in claims if claim is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].job.id, job.id)
        current = await self.repo.get_job(job.id)
        self.assertEqual(current.state, "downloading")

    async def test_publish_claim_waits_for_earlier_download(self) -> None:
        first = await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="queued",
            source_kind="url",
            event_payload={"schema_version": 1},
        )
        second = await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="ready",
            source_kind="url",
            event_payload={"schema_version": 1},
        )
        self.assertIsNone(await self.repo.claim_next_publish("publisher"))
        current = await self.repo.get_job(first.id)
        await self.repo.transition_job(
            first.id,
            expected_revision=current.revision,
            to_state="cancelled",
            event_type="cancelled",
            payload={"schema_version": 1},
        )
        claim = await self.repo.claim_next_publish("publisher")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.job.id, second.id)
        self.assertEqual(claim.job.state, "publishing")

    async def test_concurrent_publish_claim_keeps_fifo_single_winner(self) -> None:
        first = await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="ready",
            source_kind="url",
            event_payload={"schema_version": 1},
        )
        await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="ready",
            source_kind="url",
            event_payload={"schema_version": 1},
        )
        other = SQLiteRepository(self.db_path, download_root=self.download_root)
        await other.open()
        try:
            claims = await asyncio.gather(
                self.repo.claim_next_publish("publisher-a"),
                other.claim_next_publish("publisher-b"),
            )
        finally:
            await other.close()
        winners = [claim for claim in claims if claim is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].job.id, first.id)

    async def test_stale_publish_cas_does_not_duplicate_refs_or_event(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="publishing",
            source_kind="url",
            event_payload={"schema_version": 1},
        )
        result = await self.repo.record_published_messages(
            job.id,
            [(-1001, 9, "channel")],
            expected_revision=job.revision,
        )
        self.assertTrue(result.applied)
        stale = await self.repo.record_published_messages(
            job.id,
            [(-1001, 9, "channel")],
            expected_revision=job.revision,
        )
        self.assertFalse(stale.applied)
        refs = await self.repo.list_published_messages(job.id)
        self.assertEqual([(ref.peer_id, ref.message_id) for ref in refs], [(-1001, 9)])
        events = await self.repo.list_job_events(job.id)
        self.assertEqual([event.event_type for event in events], ["accepted", "published"])

    async def test_publish_checkpoint_is_idempotent_and_does_not_finish_job(self) -> None:
        job = await self.repo.accept_job(
            kind="collection", user_id=1, state="publishing", source_kind="telegram",
            event_payload={"schema_version": 1},
        )
        inserted = await self.repo.checkpoint_published_messages(
            job.id, [(-1001, 11, "cover"), (-1002, 12, "comment")]
        )
        repeated = await self.repo.checkpoint_published_messages(
            job.id, [(-1001, 11, "cover")]
        )
        current = await self.repo.get_job(job.id)
        refs = await self.repo.list_published_messages(job.id)
        self.assertEqual(inserted, 2)
        self.assertEqual(repeated, 0)
        self.assertEqual(current.state, "publishing")
        self.assertEqual(current.revision, job.revision)
        self.assertEqual([(item.peer_id, item.message_id) for item in refs], [(-1001, 11), (-1002, 12)])
        events = await self.repo.list_job_events(job.id)
        self.assertEqual([event.event_type for event in events], ["accepted", "publish_checkpoint"])

    async def test_publish_checkpoint_distinguishes_same_message_id_across_peers(self) -> None:
        job = await self.repo.accept_job(
            kind="collection", user_id=1, state="publishing", source_kind="telegram",
            event_payload={"schema_version": 1},
        )
        inserted = await self.repo.checkpoint_published_messages(
            job.id, [(-1001, 11, "cover"), (-1002, 11, "comment")]
        )
        self.assertEqual(inserted, 2)
        refs = await self.repo.list_published_messages(job.id)
        self.assertEqual(
            [(item.peer_id, item.message_id, item.role) for item in refs],
            [(-1001, 11, "cover"), (-1002, 11, "comment")],
        )

    async def test_interaction_session_revisioned_crud(self) -> None:
        record = await self.repo.upsert_interaction_session(
            user_id=42,
            kind="webdav",
            field="url",
            payload={"schema_version": 1, "step": "url"},
            revision=3,
            expires_at=999.0,
        )
        self.assertEqual(record.revision, 3)
        self.assertFalse(await self.repo.delete_interaction_session(42, revision=2))
        self.assertTrue(await self.repo.delete_interaction_session(42, revision=3))

    async def test_backup_files_now_reference_job_items(self) -> None:
        raw = sqlite3.connect(self.db_path)
        try:
            fks = raw.execute("PRAGMA foreign_key_list(backup_files)").fetchall()
        finally:
            raw.close()
        self.assertTrue(any(row[2] == "job_items" and row[3] == "job_item_id" for row in fks))

    async def test_backup_retry_state_and_due_window_are_durable(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="succeeded",
            source_kind="url",
            source_url="https://example.invalid/backup",
            legacy_seq=701,
            event_payload={"schema_version": 1},
        )
        attempt = await self.repo.ensure_backup_attempt(
            job_id=job.id,
            remote_dir="backup/retry",
        )
        local = self.download_root / "job-701" / "a.mp4"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"abc")
        file = await self.repo.ensure_backup_file(
            attempt_id=attempt.id,
            local_path=str(local),
            remote_name="hash.mp4",
            size_bytes=3,
        )
        await self.repo.update_backup_file_status(
            file.id,
            state="failed",
            error_code="webdav_server",
            error_message="safe",
        )
        await self.repo.update_backup_attempt_status(
            attempt.id,
            state="retry_wait",
            retry_count=1,
            next_retry_at=200.0,
            error_code="webdav_server",
            error_message="safe",
            now=100.0,
        )
        self.assertEqual(
            await self.repo.backup_retry_due(
                legacy_seq=701,
                remote_dir="backup/retry",
                now=199.0,
            ),
            (False, 200.0),
        )
        self.assertEqual(
            await self.repo.backup_retry_due(
                legacy_seq=701,
                remote_dir="backup/retry",
                now=200.0,
            ),
            (True, 200.0),
        )
        attempts = await self.repo.list_backup_attempts(job.id)
        self.assertEqual(
            (attempts[-1].state, attempts[-1].retry_count, attempts[-1].next_retry_at),
            ("retry_wait", 1, 200.0),
        )

    async def test_upgrade_from_schema_one_preserves_existing_rows_and_creates_backup(self) -> None:
        await self.repo.close()
        root = Path(self.tempdir.name)
        migration_dir = root / "upgrade-migrations"
        migration_dir.mkdir()
        source_dir = Path(__file__).parents[1] / "src" / "repository" / "migrations"
        (migration_dir / "0001_initial.sql").write_bytes(
            (source_dir / "0001_initial.sql").read_bytes()
        )
        db = root / "upgrade.sqlite3"
        first = SQLiteRepository(db, migrations_dir=migration_dir, backup_dir=self.backup_dir)
        await first.open()
        await first.migrate()
        job = await first.create_job(
            kind="url",
            user_id=77,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/keep",
            event_payload={"schema_version": 1},
        )
        await first.close()

        (migration_dir / "0002_runtime_entities.sql").write_bytes(
            (source_dir / "0002_runtime_entities.sql").read_bytes()
        )
        second = SQLiteRepository(db, migrations_dir=migration_dir, backup_dir=self.backup_dir)
        await second.open()
        try:
            self.assertEqual(await second.migrate(), [2])
            self.assertIsNotNone(second.last_backup_path)
            self.assertTrue(second.last_backup_path.exists())
            preserved = await second.get_job(job.id)
            self.assertIsNotNone(preserved)
            self.assertEqual(preserved.source_url, "https://example.invalid/keep")
            self.assertEqual(await second.schema_versions(), [1, 2])
        finally:
            await second.close()

        self.repo = SQLiteRepository(self.db_path, backup_dir=self.backup_dir)
        await self.repo.open()
        await self.repo.migrate()

    async def test_upgrade_from_schema_two_preserves_rows_and_adds_claim_columns(self) -> None:
        await self.repo.close()
        root = Path(self.tempdir.name)
        migration_dir = root / "upgrade-r3-migrations"
        migration_dir.mkdir()
        source_dir = Path(__file__).parents[1] / "src" / "repository" / "migrations"
        for name in ("0001_initial.sql", "0002_runtime_entities.sql"):
            (migration_dir / name).write_bytes((source_dir / name).read_bytes())
        db = root / "upgrade-r3.sqlite3"
        first = SQLiteRepository(db, migrations_dir=migration_dir, backup_dir=self.backup_dir)
        await first.open()
        await first.migrate()
        job = await first.accept_job(
            kind="url",
            user_id=88,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/r3",
            event_payload={"schema_version": 1},
        )
        await first.close()

        (migration_dir / "0003_claims.sql").write_bytes(
            (source_dir / "0003_claims.sql").read_bytes()
        )
        second = SQLiteRepository(db, migrations_dir=migration_dir, backup_dir=self.backup_dir)
        await second.open()
        try:
            self.assertEqual(await second.migrate(), [3])
            self.assertIsNotNone(second.last_backup_path)
            self.assertTrue(second.last_backup_path.exists())
            self.assertEqual((await second.get_job(job.id)).source_url, "https://example.invalid/r3")
            self.assertEqual(await second.schema_versions(), [1, 2, 3])
            raw = sqlite3.connect(db)
            try:
                columns = {row[1] for row in raw.execute("PRAGMA table_info(jobs)").fetchall()}
            finally:
                raw.close()
            self.assertTrue({"claim_owner", "claim_kind", "heartbeat_at"}.issubset(columns))
        finally:
            await second.close()

        self.repo = SQLiteRepository(
            self.db_path,
            backup_dir=self.backup_dir,
            download_root=self.download_root,
        )
        await self.repo.open()
        await self.repo.migrate()

    async def test_upgrade_from_schema_three_preserves_rows_and_adds_legacy_seq(self) -> None:
        await self.repo.close()
        root = Path(self.tempdir.name)
        migration_dir = root / "upgrade-r3b-migrations"
        migration_dir.mkdir()
        source_dir = Path(__file__).parents[1] / "src" / "repository" / "migrations"
        for name in ("0001_initial.sql", "0002_runtime_entities.sql", "0003_claims.sql"):
            (migration_dir / name).write_bytes((source_dir / name).read_bytes())
        db = root / "upgrade-r3b.sqlite3"
        first = SQLiteRepository(db, migrations_dir=migration_dir, backup_dir=self.backup_dir)
        await first.open()
        await first.migrate()
        job = await first.accept_job(
            kind="url",
            user_id=99,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/r3b",
            event_payload={"schema_version": 1},
        )
        await first.close()

        (migration_dir / "0004_recovery.sql").write_bytes(
            (source_dir / "0004_recovery.sql").read_bytes()
        )
        second = SQLiteRepository(db, migrations_dir=migration_dir, backup_dir=self.backup_dir)
        await second.open()
        try:
            self.assertEqual(await second.migrate(), [4])
            self.assertIsNotNone(second.last_backup_path)
            self.assertTrue(second.last_backup_path.exists())
            preserved = await second.get_job(job.id)
            self.assertEqual(preserved.source_url, "https://example.invalid/r3b")
            self.assertIsNone(preserved.legacy_seq)
            self.assertEqual(await second.schema_versions(), [1, 2, 3, 4])
        finally:
            await second.close()

        self.repo = SQLiteRepository(
            self.db_path,
            backup_dir=self.backup_dir,
            download_root=self.download_root,
        )
        await self.repo.open()
        await self.repo.migrate()

    async def test_upgrade_from_schema_four_adds_stats_and_reconciles_existing_jobs(self) -> None:
        await self.repo.close()
        root = Path(self.tempdir.name)
        migration_dir = root / "upgrade-f4-migrations"
        migration_dir.mkdir()
        source_dir = Path(__file__).parents[1] / "src" / "repository" / "migrations"
        for name in (
            "0001_initial.sql",
            "0002_runtime_entities.sql",
            "0003_claims.sql",
            "0004_recovery.sql",
        ):
            (migration_dir / name).write_bytes((source_dir / name).read_bytes())

        db = root / "upgrade-f4.sqlite3"
        first = SQLiteRepository(
            db,
            migrations_dir=migration_dir,
            backup_dir=self.backup_dir,
            download_root=self.download_root,
        )
        await first.open()
        await first.migrate()
        job = await first.accept_job(
            kind="url",
            user_id=7,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/pre-f4",
            legacy_seq=700,
            event_payload={"schema_version": 1},
        )
        await first.close()

        (migration_dir / "0005_stats.sql").write_bytes(
            (source_dir / "0005_stats.sql").read_bytes()
        )
        second = SQLiteRepository(
            db,
            migrations_dir=migration_dir,
            backup_dir=self.backup_dir,
            download_root=self.download_root,
        )
        await second.open()
        try:
            self.assertEqual(await second.migrate(), [5])
            self.assertIsNotNone(second.last_backup_path)
            self.assertTrue(second.last_backup_path.exists())
            preserved = await second.get_job(job.id)
            self.assertEqual(preserved.source_url, "https://example.invalid/pre-f4")
            self.assertEqual(await second.schema_versions(), [1, 2, 3, 4, 5])
            self.assertGreaterEqual(await second.reconcile_daily_stats(), 1)
            snapshot = await second.stats_snapshot()
            self.assertEqual(snapshot["totals"]["accepted_jobs"], 1)
            self.assertEqual(await second.reconcile_daily_stats(), 0)
        finally:
            await second.close()

        self.repo = SQLiteRepository(
            self.db_path,
            backup_dir=self.backup_dir,
            download_root=self.download_root,
        )
        await self.repo.open()
        await self.repo.migrate()

    async def test_job_event_backup_and_setting_crud(self) -> None:
        job = await self.repo.create_job(
            kind="url",
            user_id=42,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/video",
            event_payload={"schema_version": 1, "source": "test"},
            now=123.0,
        )
        self.assertEqual(job.id, 1)
        self.assertEqual((await self.repo.get_job(job.id)).source_url, job.source_url)
        self.assertEqual([item.id for item in await self.repo.list_jobs(user_id=42)], [job.id])
        await self.repo.set_status_reference(job.id, chat_id=42, message_id=9001)
        current = await self.repo.get_job(job.id)
        self.assertEqual((current.status_chat_id, current.status_message_id), (42, 9001))
        await self.repo.update_job_progress(
            job.id,
            bytes_done=128,
            bytes_total=1024,
            current_item=1,
            total_items=2,
        )
        raw = sqlite3.connect(self.db_path)
        try:
            progress = raw.execute(
                "SELECT bytes_done,bytes_total,current_item,total_items FROM jobs WHERE id=?",
                (job.id,),
            ).fetchone()
        finally:
            raw.close()
        # queued jobs do not accept transport progress writes.
        self.assertEqual(progress, (0, 0, 0, 0))
        events = await self.repo.list_job_events(job.id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "accepted")
        self.assertIn('"schema_version":1', events[0].payload_json)

        attempt = await self.repo.create_backup_attempt(
            job_id=job.id,
            state="pending",
            remote_dir="/backup/1",
            now=124.0,
        )
        backup_file = await self.repo.create_backup_file(
            attempt_id=attempt.id,
            local_path=str(self.download_root / "job-1" / "test.mp4"),
            remote_name="hash.mp4",
            size_bytes=99,
        )
        self.assertEqual(backup_file.attempt_id, attempt.id)
        self.assertEqual(backup_file.size_bytes, 99)

        self.assertEqual(await self.repo.set_setting("example", {"enabled": True}), 1)
        self.assertEqual(await self.repo.set_setting("example", {"enabled": False}), 2)
        self.assertEqual(await self.repo.get_setting("example"), {"enabled": False})
        self.assertEqual(await self.repo.get_setting("missing", default="fallback"), "fallback")

    async def test_u1_status_progress_and_state_counts(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="queued",
            source_kind="url",
            source_url="https://example.invalid/u1",
            event_payload={"schema_version": 1},
        )
        await self.repo.accept_job(
            kind="url",
            user_id=42,
            state="failed",
            source_kind="url",
            source_url="https://example.invalid/failed",
            event_payload={"schema_version": 1},
        )
        claim = await self.repo.claim_next_download("u1-worker")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.job.id, job.id)
        await self.repo.set_status_reference(job.id, chat_id=42, message_id=9010)
        await self.repo.update_job_progress(
            job.id,
            bytes_done=64,
            bytes_total=256,
            current_item=2,
            total_items=4,
        )
        raw = sqlite3.connect(self.db_path)
        try:
            values = raw.execute(
                "SELECT status_chat_id,status_message_id,bytes_done,bytes_total,current_item,total_items FROM jobs WHERE id=?",
                (job.id,),
            ).fetchone()
        finally:
            raw.close()
        self.assertEqual(values, (42, 9010, 64, 256, 2, 4))
        counts = await self.repo.count_jobs_by_state(user_id=42)
        self.assertEqual(counts.get("downloading"), 1)
        self.assertEqual(counts.get("failed"), 1)

    async def test_payload_version_and_backup_path_are_validated(self) -> None:
        with self.assertRaisesRegex(Exception, "schema_version"):
            await self.repo.create_job(
                kind="url",
                user_id=42,
                state="queued",
                source_kind="url",
                event_payload={"source": "missing-version"},
            )
        job = await self.repo.create_job(
            kind="url",
            user_id=42,
            state="queued",
            source_kind="url",
        )
        attempt = await self.repo.create_backup_attempt(
            job_id=job.id,
            state="pending",
            remote_dir="/backup/path-check",
        )
        with self.assertRaisesRegex(Exception, "outside download root"):
            await self.repo.create_backup_file(
                attempt_id=attempt.id,
                local_path=str(Path(self.tempdir.name).parent / "escape.mp4"),
                remote_name="escape.mp4",
                size_bytes=1,
            )

    async def test_create_job_rolls_back_when_event_insert_fails(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            await self.repo.create_job(
                kind="url",
                user_id=42,
                state="queued",
                source_kind="url",
                event_type=None,  # type: ignore[arg-type]
                now=125.0,
            )
        self.assertEqual(await self.repo.list_jobs(), [])

    async def test_foreign_keys_reject_orphan_backup_attempt(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            await self.repo.create_backup_attempt(
                job_id=999,
                state="pending",
                remote_dir="/missing",
            )

    async def test_applied_migration_checksum_change_fails_closed(self) -> None:
        await self.repo.close()
        migration_dir = Path(self.tempdir.name) / "migrations"
        migration_dir.mkdir()
        source = Path(__file__).parents[1] / "src" / "repository" / "migrations" / "0001_initial.sql"
        target = migration_dir / source.name
        target.write_bytes(source.read_bytes())

        db = Path(self.tempdir.name) / "checksum.sqlite3"
        first = SQLiteRepository(db, migrations_dir=migration_dir)
        await first.open()
        await first.migrate()
        await first.close()
        target.write_text(target.read_text() + "\n-- changed after apply\n")

        second = SQLiteRepository(db, migrations_dir=migration_dir)
        await second.open()
        try:
            with self.assertRaises(MigrationChecksumError):
                await second.migrate()
        finally:
            await second.close()

    async def test_failed_migration_rolls_back_and_existing_db_gets_backup(self) -> None:
        await self.repo.close()
        migration_dir = Path(self.tempdir.name) / "bad-migrations"
        migration_dir.mkdir()
        (migration_dir / "0001_initial.sql").write_text(
            "CREATE TABLE base(id INTEGER PRIMARY KEY);\n"
        )
        db = Path(self.tempdir.name) / "rollback.sqlite3"
        first = SQLiteRepository(db, migrations_dir=migration_dir, backup_dir=self.backup_dir)
        await first.open()
        await first.migrate()
        await first.close()

        (migration_dir / "0002_bad.sql").write_text(
            "CREATE TABLE should_rollback(id INTEGER PRIMARY KEY);\n"
            "INSERT INTO missing_table(value) VALUES (1);\n"
        )
        second = SQLiteRepository(db, migrations_dir=migration_dir, backup_dir=self.backup_dir)
        await second.open()
        try:
            with self.assertRaises(MigrationError):
                await second.migrate()
            self.assertIsNotNone(second.last_backup_path)
            self.assertTrue(second.last_backup_path.exists())
        finally:
            await second.close()

        raw = sqlite3.connect(db)
        try:
            names = {
                row[0]
                for row in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            versions = raw.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            raw.close()
        self.assertNotIn("should_rollback", names)
        self.assertEqual(versions, [(1,)])
