import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.repository import MigrationChecksumError, MigrationError, SQLiteRepository
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
        self.assertEqual(await self.repo.schema_versions(), [1, 2, 3, 4])
        self.assertEqual(await self.repo.migrate(), [])
        check = await self.repo.self_check()
        self.assertEqual(check["integrity"], "ok")
        self.assertEqual(check["foreign_keys"], 1)
        self.assertGreaterEqual(check["busy_timeout"], 5000)
        self.assertEqual(check["synchronous"], 2)
        self.assertTrue(SQLiteRepository.REQUIRED_TABLES.issubset(check["tables"]))

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
