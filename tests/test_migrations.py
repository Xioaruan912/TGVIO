from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.rehearse_migration import RehearsalError, rehearse
from tgvio.infrastructure.migration_runner import (
    MigrationChecksumError,
    MigrationRunner,
    SchemaFingerprintError,
)
from tgvio.infrastructure.schema import (
    TGVIO_BASELINE_SCHEMA_SQL_SHA256,
    schema_identity,
    schema_sql_sha256,
)
from tgvio.infrastructure.sqlite import SQLiteJobRepository


MIGRATIONS_DIR = Path(__file__).parents[1] / "src" / "tgvio" / "infrastructure" / "migrations"
BASELINE_SQL = MIGRATIONS_DIR / "0001_baseline.sql"
SCHEDULER_SQL = MIGRATIONS_DIR / "0002_scheduler.sql"
LATEST_VERSION = 3
MIGRATION_ONLY_TABLES = {
    "runtime_leases",
    "job_phase_claims",
    "job_schedule",
    "intake_events",
    "collection_sessions",
    "collection_entries",
    "user_preferences",
    "job_display_messages",
}


def _business_snapshot(connection: sqlite3.Connection) -> dict[str, object]:
    snapshot = schema_identity(connection).snapshot
    return {
        "tables": [
            table
            for table in snapshot["tables"]  # type: ignore[index]
            if table["name"] not in MIGRATION_ONLY_TABLES
        ],
        "views": snapshot["views"],
        "triggers": snapshot["triggers"],
    }


def _legacy_baseline_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(BASELINE_SQL.read_text(encoding="utf-8"))
        connection.execute(
            """
            INSERT INTO jobs(id, owner_id, destination, state, policy_json)
            VALUES('legacy-job', 7, '@channel', 'succeeded', '{}')
            """
        )
        connection.commit()
    finally:
        connection.close()


def _version1_database(path: Path) -> None:
    migrations = path.parent / "v1-migrations"
    migrations.mkdir()
    shutil.copy2(BASELINE_SQL, migrations / BASELINE_SQL.name)
    MigrationRunner(path, migrations_dir=migrations).run()
    connection = sqlite3.connect(path)
    try:
        for job_id, created_at in (
            ("v1-job-c", "2026-09-12 03:00:00"),
            ("v1-job-a", "2026-09-12 01:00:00"),
            ("v1-job-b", "2026-09-12 02:00:00"),
        ):
            connection.execute(
                """
                INSERT INTO jobs(id, owner_id, destination, state, policy_json, created_at, updated_at)
                VALUES(?, 7, '@channel', 'succeeded', '{}', ?, ?)
                """,
                (job_id, created_at, created_at),
            )
        connection.commit()
    finally:
        connection.close()


def _version2_database(path: Path) -> None:
    migrations = path.parent / "v2-migrations"
    migrations.mkdir()
    shutil.copy2(BASELINE_SQL, migrations / BASELINE_SQL.name)
    shutil.copy2(SCHEDULER_SQL, migrations / SCHEDULER_SQL.name)
    MigrationRunner(path, migrations_dir=migrations).run()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO jobs(id, owner_id, destination, state, policy_json)
            VALUES('v2-job', 7, '@channel', 'succeeded', '{}')
            """
        )
        connection.execute("INSERT INTO job_schedule(job_id) VALUES('v2-job')")
        connection.commit()
    finally:
        connection.close()


def _production_shape_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(BASELINE_SQL.read_text(encoding="utf-8"))
        states = ["succeeded"] * 16 + ["cancelled"] * 4 + ["failed"] * 3
        for index, state in enumerate(states):
            job_id = f"prod-shape-job-{index:02d}"
            connection.execute(
                """
                INSERT INTO jobs(id, owner_id, destination, state, policy_json)
                VALUES(?,?,?,?, '{}')
                """,
                (job_id, 1000 + index, "@channel", state),
            )
            connection.execute(
                """
                INSERT INTO job_progress(job_id, phase, current_value, total_value, item_total)
                VALUES(?,?,?,?,?)
                """,
                (job_id, state, 1, 1, 1),
            )

        connection.execute(
            """
            INSERT INTO publish_plans(id, job_id, version, summary_json)
            VALUES('prod-shape-plan', 'prod-shape-job-00', 1, '{}')
            """
        )
        publish_states = ["succeeded"] * 40 + ["failed", "pending"]
        for step_index, state in enumerate(publish_states):
            connection.execute(
                """
                INSERT INTO publish_steps(
                    plan_id, step_index, kind, target, item_indexes_json,
                    params_json, state
                ) VALUES('prod-shape-plan', ?, 'channel_cover_album', 'channel', '[]', '{}', ?)
                """,
                (step_index, state),
            )

        package_states = ["committed"] * 16 + ["failed"] * 4
        for index, state in enumerate(package_states):
            package_id = f"prod-shape-package-{index:02d}"
            connection.execute(
                """
                INSERT INTO archive_packages(
                    id, job_id, layout_version, remote_path, staging_path,
                    state, manifest_json
                ) VALUES(?,?,?,?,?,?, '{}')
                """,
                (
                    package_id,
                    f"prod-shape-job-{index:02d}",
                    "v1",
                    f"archive/{index:02d}",
                    f"staging/{index:02d}",
                    state,
                ),
            )

        object_states = ["stored"] * 147 + ["pending"] * 29 + ["failed"] * 3
        for object_index, state in enumerate(object_states):
            connection.execute(
                """
                INSERT INTO archive_objects(
                    package_id, object_index, item_index, role, local_path,
                    remote_relpath, size_bytes, sha256, state
                ) VALUES(
                    'prod-shape-package-00', ?, ?, 'media', ?, ?, 1, ?, ?
                )
                """,
                (
                    object_index,
                    object_index,
                    f"/fixture/media-{object_index}",
                    f"media/{object_index}",
                    f"sha-{object_index:03d}",
                    state,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _production_shape_counts(connection: sqlite3.Connection) -> dict[str, object]:
    return {
        "jobs": connection.execute(
            "SELECT state, COUNT(*) FROM jobs GROUP BY state ORDER BY state"
        ).fetchall(),
        "publish_steps": connection.execute(
            "SELECT state, COUNT(*) FROM publish_steps GROUP BY state ORDER BY state"
        ).fetchall(),
        "archive_packages": connection.execute(
            "SELECT state, COUNT(*) FROM archive_packages GROUP BY state ORDER BY state"
        ).fetchall(),
        "archive_objects": connection.execute(
            "SELECT state, COUNT(*) FROM archive_objects GROUP BY state ORDER BY state"
        ).fetchall(),
        "job_progress": connection.execute("SELECT COUNT(*) FROM job_progress").fetchone()[0],
    }


class MigrationRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_baseline_fixture_matches_recorded_production_schema_hash(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(BASELINE_SQL.read_text(encoding="utf-8"))
            self.assertEqual(schema_sql_sha256(connection), TGVIO_BASELINE_SCHEMA_SQL_SHA256)
            identity = schema_identity(connection)
            self.assertTrue(identity.fingerprint)
            table_names = {row["name"] for row in identity.snapshot["tables"]}  # type: ignore[index]
            self.assertIn("jobs", table_names)
            self.assertIn("publish_effects", table_names)
            self.assertIn("archive_objects", table_names)
            self.assertIn("runtime_health", table_names)
            self.assertNotIn("schema_migrations", table_names)
        finally:
            connection.close()

    async def test_fresh_database_applies_baseline_and_records_checksum_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.sqlite3"
            repo = SQLiteJobRepository(database)
            await repo.open()
            try:
                status = repo.schema_status()
                self.assertEqual(status["recorded_versions"], [1, 2, 3])
                self.assertEqual(status["applied_now"], [1, 2, 3])
                self.assertEqual(status["user_version"], LATEST_VERSION)
                self.assertTrue(status["ledger_present"])
                self.assertFalse(status["backup_created"])
            finally:
                await repo.close()

            connection = sqlite3.connect(database)
            try:
                rows = connection.execute(
                    "SELECT version, name, length(checksum) FROM schema_migrations ORDER BY version"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        (1, "baseline", 64),
                        (2, "scheduler", 64),
                        (3, "intake_collections", 64),
                    ],
                )
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], LATEST_VERSION)
            finally:
                connection.close()

    async def test_existing_baseline_is_registered_without_rebuilding_or_moving_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "state.sqlite3"
            backups = root / "backups"
            _legacy_baseline_database(database)
            before = sqlite3.connect(database)
            try:
                before_snapshot = _business_snapshot(before)
                before_counts = {
                    "jobs": before.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                    "publish_steps": before.execute("SELECT COUNT(*) FROM publish_steps").fetchone()[0],
                    "archive_packages": before.execute("SELECT COUNT(*) FROM archive_packages").fetchone()[0],
                }
                self.assertFalse(
                    before.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_migrations'"
                    ).fetchone()
                )
            finally:
                before.close()

            repo = SQLiteJobRepository(database, backup_dir=backups)
            await repo.open()
            try:
                status = repo.schema_status()
                self.assertEqual(status["applied_now"], [1, 2, 3])
                self.assertTrue(status["backup_created"])
                loaded = await repo.get("legacy-job")
                self.assertIsNotNone(loaded)
            finally:
                await repo.close()

            after = sqlite3.connect(database)
            try:
                after_snapshot = _business_snapshot(after)
                after_counts = {
                    "jobs": after.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                    "publish_steps": after.execute("SELECT COUNT(*) FROM publish_steps").fetchone()[0],
                    "archive_packages": after.execute("SELECT COUNT(*) FROM archive_packages").fetchone()[0],
                }
                self.assertEqual(before_snapshot, after_snapshot)
                self.assertEqual(before_counts, after_counts)
                self.assertEqual(after.execute("PRAGMA user_version").fetchone()[0], LATEST_VERSION)
                self.assertEqual(after.execute("SELECT COUNT(*) FROM job_schedule").fetchone()[0], 1)
            finally:
                after.close()

            backup_files = list(backups.glob("*.sqlite3"))
            self.assertEqual(len(backup_files), 1)
            backup = sqlite3.connect(backup_files[0])
            try:
                self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                self.assertIsNone(
                    backup.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_migrations'"
                    ).fetchone()
                )
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
            finally:
                backup.close()

    async def test_production_shape_counts_and_states_are_identical_after_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "state.sqlite3"
            backups = root / "backups"
            _production_shape_database(database)

            before = sqlite3.connect(database)
            try:
                before_counts = _production_shape_counts(before)
                before_snapshot = _business_snapshot(before)
                self.assertEqual(
                    before_counts["jobs"],
                    [("cancelled", 4), ("failed", 3), ("succeeded", 16)],
                )
                self.assertEqual(
                    before_counts["publish_steps"],
                    [("failed", 1), ("pending", 1), ("succeeded", 40)],
                )
                self.assertEqual(
                    before_counts["archive_packages"],
                    [("committed", 16), ("failed", 4)],
                )
                self.assertEqual(
                    before_counts["archive_objects"],
                    [("failed", 3), ("pending", 29), ("stored", 147)],
                )
                self.assertEqual(before_counts["job_progress"], 23)
            finally:
                before.close()

            repo = SQLiteJobRepository(database, backup_dir=backups)
            await repo.open()
            try:
                status = repo.schema_status()
                self.assertEqual(status["recorded_versions"], [1, 2, 3])
                self.assertEqual(status["applied_now"], [1, 2, 3])
                self.assertTrue(status["backup_created"])
            finally:
                await repo.close()

            after = sqlite3.connect(database)
            try:
                self.assertEqual(_production_shape_counts(after), before_counts)
                self.assertEqual(_business_snapshot(after), before_snapshot)
                self.assertEqual(after.execute("PRAGMA user_version").fetchone()[0], LATEST_VERSION)
                self.assertEqual(after.execute("SELECT COUNT(*) FROM job_schedule").fetchone()[0], 23)
                self.assertEqual(after.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                after.close()

            backup_files = list(backups.glob("*.sqlite3"))
            self.assertEqual(len(backup_files), 1)
            backup = sqlite3.connect(backup_files[0])
            try:
                self.assertEqual(_production_shape_counts(backup), before_counts)
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertIsNone(
                    backup.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_migrations'"
                    ).fetchone()
                )
            finally:
                backup.close()

    async def test_rehearsal_tool_reports_only_safe_aggregate_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "production-copy.sqlite3"
            backups = root / "backups"
            _production_shape_database(database)

            report = rehearse(database, backups)
            self.assertEqual(report["status"], "passed")
            before = report["before"]
            after = report["after"]
            assert isinstance(before, dict) and isinstance(after, dict)
            self.assertEqual(before["user_version"], 0)
            self.assertEqual(after["user_version"], LATEST_VERSION)
            self.assertFalse(before["migration_ledger_present"])
            self.assertTrue(after["migration_ledger_present"])
            self.assertEqual(before["jobs"], after["jobs"])
            self.assertNotIn("path", report)
            serialized = str(report)
            self.assertNotIn("prod-shape-job", serialized)
            self.assertNotIn("@channel", serialized)
            self.assertNotIn("/fixture/", serialized)

            with self.assertRaises(RehearsalError):
                rehearse(Path("/root/TGVIO/data/state.sqlite3"), root / "blocked-backups")

    async def test_rehearsal_tool_supports_v1_to_latest_forward_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "production-v1-copy.sqlite3"
            backups = root / "backups"
            _version1_database(database)

            report = rehearse(database, backups)
            self.assertEqual(report["status"], "passed")
            migration = report["migration"]
            assert isinstance(migration, dict)
            self.assertEqual(migration["from_version"], 1)
            self.assertEqual(migration["to_version"], 3)
            self.assertEqual(migration["applied_now"], [2, 3])
            before = report["before"]
            after = report["after"]
            backup = report["backup"]
            assert isinstance(before, dict) and isinstance(after, dict) and isinstance(backup, dict)
            self.assertEqual(before["user_version"], 1)
            self.assertTrue(before["migration_ledger_present"])
            self.assertEqual(after["user_version"], 3)
            self.assertTrue(after["migration_ledger_present"])
            self.assertEqual(backup["user_version"], 1)
            self.assertTrue(backup["migration_ledger_present"])
            self.assertEqual(before["jobs"], after["jobs"])

    async def test_rehearsal_tool_supports_current_v2_to_v3_forward_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "production-v2-copy.sqlite3"
            backups = root / "backups"
            _version2_database(database)

            report = rehearse(database, backups)
            self.assertEqual(report["status"], "passed")
            migration = report["migration"]
            assert isinstance(migration, dict)
            self.assertEqual(migration["from_version"], 2)
            self.assertEqual(migration["to_version"], 3)
            self.assertEqual(migration["applied_now"], [3])
            before = report["before"]
            after = report["after"]
            backup = report["backup"]
            assert isinstance(before, dict) and isinstance(after, dict) and isinstance(backup, dict)
            self.assertEqual(before["user_version"], 2)
            self.assertEqual(after["user_version"], 3)
            self.assertEqual(backup["user_version"], 2)
            self.assertEqual(before["jobs"], after["jobs"])

            connection = sqlite3.connect(database)
            try:
                for table in (
                    "intake_events",
                    "collection_sessions",
                    "collection_entries",
                    "user_preferences",
                    "job_display_messages",
                ):
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
                            (table,),
                        ).fetchone()
                    )
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                connection.close()

    async def test_repeat_open_is_noop_and_does_not_create_another_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "state.sqlite3"
            backups = root / "backups"
            _legacy_baseline_database(database)
            first = SQLiteJobRepository(database, backup_dir=backups)
            await first.open()
            await first.close()
            first_backups = sorted(backups.glob("*.sqlite3"))
            self.assertEqual(len(first_backups), 1)

            second = SQLiteJobRepository(database, backup_dir=backups)
            await second.open()
            try:
                status = second.schema_status()
                self.assertEqual(status["recorded_versions"], [1, 2, 3])
                self.assertEqual(status["applied_now"], [])
                self.assertFalse(status["backup_created"])
            finally:
                await second.close()
            self.assertEqual(sorted(backups.glob("*.sqlite3")), first_backups)

    async def test_applied_migration_checksum_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migrations = root / "migrations"
            migrations.mkdir()
            shutil.copy2(BASELINE_SQL, migrations / BASELINE_SQL.name)
            database = root / "state.sqlite3"
            repo = SQLiteJobRepository(database, migrations_dir=migrations)
            await repo.open()
            await repo.close()

            baseline = migrations / BASELINE_SQL.name
            baseline.write_text(
                baseline.read_text(encoding="utf-8") + "\n-- tampered after application\n",
                encoding="utf-8",
            )
            reopened = SQLiteJobRepository(database, migrations_dir=migrations)
            with self.assertRaises(MigrationChecksumError):
                await reopened.open()

            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                connection.close()

    async def test_recorded_scheduler_schema_drift_fails_closed_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.sqlite3"
            repo = SQLiteJobRepository(database)
            await repo.open()
            await repo.close()

            connection = sqlite3.connect(database)
            connection.execute("DROP INDEX idx_job_schedule_job")
            connection.commit()
            connection.close()

            reopened = SQLiteJobRepository(database)
            with self.assertRaises(SchemaFingerprintError):
                await reopened.open()

            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(
                    connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall(),
                    [(1,), (2,), (3,)],
                )
            finally:
                connection.close()

    async def test_unknown_existing_schema_fails_before_ledger_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE unexpected(id INTEGER PRIMARY KEY, note TEXT)")
                connection.commit()
            finally:
                connection.close()

            repo = SQLiteJobRepository(database)
            with self.assertRaises(SchemaFingerprintError):
                await repo.open()

            connection = sqlite3.connect(database)
            try:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_migrations'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='unexpected'"
                    ).fetchone()
                )
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            finally:
                connection.close()

    async def test_version1_database_upgrades_to_latest_without_reordering_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "state.sqlite3"
            backups = root / "backups"
            _version1_database(database)

            repo = SQLiteJobRepository(database, backup_dir=backups)
            await repo.open()
            try:
                status = repo.schema_status()
                self.assertEqual(status["recorded_versions"], [1, 2, 3])
                self.assertEqual(status["applied_now"], [2, 3])
                self.assertEqual(status["user_version"], 3)
                orders = [
                    await repo.get_accepted_order(job_id)
                    for job_id in ("v1-job-a", "v1-job-b", "v1-job-c")
                ]
                self.assertEqual(orders, [1, 2, 3])
            finally:
                await repo.close()

            self.assertEqual(len(list(backups.glob("*.sqlite3"))), 1)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(
                    connection.execute(
                        "SELECT job_id FROM job_schedule ORDER BY accepted_order"
                    ).fetchall(),
                    [("v1-job-a",), ("v1-job-b",), ("v1-job-c",)],
                )
            finally:
                connection.close()

    async def test_failed_forward_migration_rolls_back_and_can_recover_on_next_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migrations = root / "migrations"
            migrations.mkdir()
            shutil.copy2(BASELINE_SQL, migrations / BASELINE_SQL.name)
            migration2 = migrations / "0002_probe.sql"
            migration2.write_text(
                """
                CREATE TABLE migration_probe(id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO table_that_does_not_exist(value) VALUES('boom');
                """,
                encoding="utf-8",
            )
            database = root / "state.sqlite3"
            repo = SQLiteJobRepository(database, migrations_dir=migrations)
            with self.assertRaises(sqlite3.DatabaseError):
                await repo.open()

            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall(),
                    [(1,)],
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='migration_probe'"
                    ).fetchone()
                )
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            finally:
                connection.close()

            migration2.write_text(
                "CREATE TABLE migration_probe(id INTEGER PRIMARY KEY, value TEXT NOT NULL);\n",
                encoding="utf-8",
            )
            recovered = SQLiteJobRepository(database, migrations_dir=migrations)
            await recovered.open()
            try:
                status = recovered.schema_status()
                self.assertEqual(status["recorded_versions"], [1, 2])
                self.assertEqual(status["applied_now"], [2])
                self.assertEqual(status["user_version"], 2)
            finally:
                await recovered.close()

            connection = sqlite3.connect(database)
            try:
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='migration_probe'"
                    ).fetchone()
                )
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
