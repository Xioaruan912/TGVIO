from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sqlite3

from tgvio_player.infrastructure.legacy_migration_checksums import LEGACY_MIGRATIONS


_MIGRATION_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
_LEDGER_SQL = """
CREATE TABLE player_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@dataclass(frozen=True, slots=True)
class PlayerMigration:
    version: int
    name: str
    checksum: str
    sql: str


class PlayerMigrationError(RuntimeError):
    pass


def load_migrations(directory: Path) -> tuple[PlayerMigration, ...]:
    migrations: list[PlayerMigration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_RE.fullmatch(path.name)
        if match is None:
            raise PlayerMigrationError(f"invalid player migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            PlayerMigration(
                version=int(match.group("version")),
                name=match.group("name"),
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )
    if not migrations:
        raise PlayerMigrationError("no player migrations found")
    if migrations[0].version != 9 or migrations[0].name != "player_public_baseline":
        raise PlayerMigrationError("player public migration baseline 0009 is required")
    if [item.version for item in migrations] != list(range(9, len(migrations) + 9)):
        raise PlayerMigrationError("player public migration versions must be contiguous from 0009")
    return tuple(migrations)


def run_migrations(connection: sqlite3.Connection, directory: Path) -> None:
    migrations = load_migrations(directory)
    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if "player_schema_migrations" not in tables and tables:
        raise PlayerMigrationError("player database has tables without migration ledger")
    if "player_schema_migrations" not in tables:
        _apply_migration(connection, migrations[0], create_ledger=True)
        applied: tuple[tuple[int, str, str], ...] = ((
            migrations[0].version, migrations[0].name, migrations[0].checksum,
        ),)
    else:
        if len(tables) > 1:
            has_business_tables = True
        else:
            has_business_tables = False
        applied = tuple(
            (int(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT version, name, checksum FROM player_schema_migrations ORDER BY version"
            )
        )
        if not applied:
            if has_business_tables:
                raise PlayerMigrationError("player database has tables without migration history")
            _apply_migration(connection, migrations[0], create_ledger=False)
            applied = ((migrations[0].version, migrations[0].name, migrations[0].checksum),)

    if applied[0][0] == 1:
        if applied[:8] != LEGACY_MIGRATIONS or any(row[0] == 9 for row in applied):
            raise PlayerMigrationError("player legacy migration ledger is incomplete or mismatched")
        future = applied[8:]
    elif applied[0] == (9, migrations[0].name, migrations[0].checksum):
        future = applied[1:]
    else:
        raise PlayerMigrationError("player public migration baseline checksum mismatch")

    expected_future = migrations[1:]
    if len(future) > len(expected_future) or any(
        row != (item.version, item.name, item.checksum)
        for row, item in zip(future, expected_future)
    ):
        raise PlayerMigrationError("player future migration ledger is incomplete or mismatched")
    for migration in expected_future[len(future):]:
        _apply_migration(connection, migration, create_ledger=False)


def _apply_migration(
    connection: sqlite3.Connection, migration: PlayerMigration, *, create_ledger: bool
) -> None:
    # executescript commits a pending transaction first, so BEGIN must be in
    # the script; the ledger insert below stays in the same transaction.
    prefix = "BEGIN IMMEDIATE;\n" + (_LEDGER_SQL + ";\n" if create_ledger else "")
    try:
        connection.executescript(prefix + migration.sql)
        try:
            connection.execute(
                "INSERT INTO player_schema_migrations(version, name, checksum) VALUES(?,?,?)",
                (migration.version, migration.name, migration.checksum),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except Exception:
        connection.rollback()
        raise
