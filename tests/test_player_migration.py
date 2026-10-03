from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.infrastructure.migration import PlayerMigrationError, run_migrations


MIGRATIONS = Path(__file__).parents[1] / "src/tgvio_player/infrastructure/migrations"
LEGACY_ROWS = (
    (1, "player_baseline", "4cffbe1fd254b97993246c38215764f4786db22746cc49d864a542a8917d0718"),
    (2, "player_sessions_feed", "aa9c6238a54f4d7deaccef81c11f5221fd1bdf3e9d702dd3f9ce1d6c5e22d8de"),
    (3, "player_long_video_progress", "7eb8e7626277ebf924a9f2e48d0812dde342183f245e2dc08f87ed2a49166c01"),
    (4, "player_deleted_locations", "9d6a99971603ba7ae3fe93c5f8ef6777f7d92cffb7eeb82207af97010f4acf88"),
    (5, "player_webdav_favorites", "8eeb717da6a87dfba37f07886b26a792101a32f5a416a76940cc949058be2d8a"),
    (6, "favorite_delete_intents", "f8e3466308e5a1fb77305e34edd109c40031645115e16053a91d5477b8bf6b83"),
    (7, "webdav_dav_endpoint", "5aa336eccd7d2a361b372fdd442f39949424d2303c43af2060cfa8287c61dade"),
    (8, "player_media_variants", "247543875ea0abff8038aa646826132aff04f836720dbcb43dc4fb40203eeefd"),
)


def ledger_connection(rows: tuple[tuple[int, str, str], ...] = LEGACY_ROWS) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE player_schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.executemany(
        "INSERT INTO player_schema_migrations(version,name,checksum) VALUES(?,?,?)",
        rows,
    )
    connection.commit()
    return connection


class PlayerMigrationTests(unittest.TestCase):
    def test_fresh_database_uses_public_baseline_only(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            run_migrations(connection, MIGRATIONS)
            self.assertEqual(
                [tuple(row) for row in connection.execute(
                    "SELECT version,name FROM player_schema_migrations ORDER BY version"
                )],
                [(9, "player_public_baseline"), (10, "media_covers"), (11, "player_collections"),
                 (12, "media_covers_phash")],
            )
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {"media", "media_variants", "media_covers", "player_storage_settings", "favorite_sync",
                 "collections", "collection_items"} <= tables
            )
            settings = connection.execute(
                "SELECT endpoint_url,player_root,favorites_dir FROM player_storage_settings"
            ).fetchone()
            self.assertEqual(tuple(settings), ("https://webdav.example.invalid/dav", "Player", "Favorites"))
        finally:
            connection.close()

    def test_both_lineages_apply_future_migration_once(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "0009_player_public_baseline.sql").write_text(
                "CREATE TABLE base_marker(id INTEGER PRIMARY KEY);\n", encoding="utf-8"
            )
            (directory / "0010_probe.sql").write_text(
                "CREATE TABLE probe(id INTEGER PRIMARY KEY);\n", encoding="utf-8"
            )
            for lineage in ("legacy", "public"):
                with self.subTest(lineage=lineage):
                    connection = ledger_connection() if lineage == "legacy" else sqlite3.connect(":memory:")
                    connection.row_factory = sqlite3.Row
                    try:
                        run_migrations(connection, directory)
                        run_migrations(connection, directory)
                        versions = [int(row[0]) for row in connection.execute(
                            "SELECT version FROM player_schema_migrations ORDER BY version"
                        )]
                        self.assertEqual(versions, list(range(1, 9)) + [10] if lineage == "legacy" else [9, 10])
                        self.assertIsNotNone(connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='probe'"
                        ).fetchone())
                    finally:
                        connection.close()

    def test_future_migration_gap_and_mutation_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "0009_player_public_baseline.sql").write_text("SELECT 1;\n", encoding="utf-8")
            future = directory / "0010_probe.sql"
            future.write_text("CREATE TABLE probe(id INTEGER PRIMARY KEY);\n", encoding="utf-8")
            connection = ledger_connection()
            try:
                run_migrations(connection, directory)
                future.write_text("CREATE TABLE changed(id INTEGER PRIMARY KEY);\n", encoding="utf-8")
                with self.assertRaises(PlayerMigrationError):
                    run_migrations(connection, directory)
            finally:
                connection.close()
            future.rename(directory / "0011_probe.sql")
            with closing(ledger_connection()) as connection:
                with self.assertRaises(PlayerMigrationError):
                    run_migrations(connection, directory)

    def test_failed_future_migration_rolls_back_schema_and_ledger(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "0009_player_public_baseline.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (directory / "0010_broken.sql").write_text(
                "CREATE TABLE transient(id INTEGER PRIMARY KEY);\n"
                "INSERT INTO absent_table(id) VALUES(1);\n", encoding="utf-8"
            )
            with closing(ledger_connection()) as connection:
                with self.assertRaises(sqlite3.Error):
                    run_migrations(connection, directory)
                self.assertIsNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='transient'"
                ).fetchone())
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM player_schema_migrations"
                ).fetchone()[0], 8)

    def test_complete_legacy_ledger_receives_every_future_migration_once(self) -> None:
        """A complete legacy ledger needs no baseline work but still gets new migrations."""
        with closing(ledger_connection()) as connection:
            after_first = connection.total_changes
            run_migrations(connection, MIGRATIONS)
            self.assertGreater(connection.total_changes, after_first)
            applied = [tuple(row) for row in connection.execute(
                "SELECT version,name,checksum FROM player_schema_migrations ORDER BY version"
            )]
            self.assertEqual(applied[:8], list(LEGACY_ROWS))
            self.assertEqual(
                [row[:2] for row in applied[8:]],
                [(10, "media_covers"), (11, "player_collections"), (12, "media_covers_phash")],
            )
            settled = connection.total_changes
            run_migrations(connection, MIGRATIONS)
            self.assertEqual(connection.total_changes, settled, "the migration must apply exactly once")
            for table in ("media_covers", "collections", "collection_items"):
                self.assertIsNotNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone())

    def test_changed_legacy_checksum_fails_closed(self) -> None:
        rows = list(LEGACY_ROWS)
        rows[4] = (5, rows[4][1], "0" * 64)
        with closing(ledger_connection(tuple(rows))) as connection:
            with self.assertRaises(PlayerMigrationError):
                run_migrations(connection, MIGRATIONS)

    def test_missing_legacy_version_fails_closed(self) -> None:
        with closing(ledger_connection(LEGACY_ROWS[:-1])) as connection:
            with self.assertRaises(PlayerMigrationError):
                run_migrations(connection, MIGRATIONS)

    def test_unknown_legacy_version_fails_closed(self) -> None:
        with closing(ledger_connection(LEGACY_ROWS + ((99, "unknown", "f" * 64),))) as connection:
            with self.assertRaises(PlayerMigrationError):
                run_migrations(connection, MIGRATIONS)

    def test_business_table_without_ledger_fails_closed(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute("CREATE TABLE media (media_id TEXT PRIMARY KEY)")
            with self.assertRaises(PlayerMigrationError):
                run_migrations(connection, MIGRATIONS)


if __name__ == "__main__":
    unittest.main()
