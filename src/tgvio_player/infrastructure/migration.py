from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sqlite3


_MIGRATION_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


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
    if [item.version for item in migrations] != list(range(1, len(migrations) + 1)):
        raise PlayerMigrationError("player migration versions must be contiguous from 0001")
    return tuple(migrations)


def run_migrations(connection: sqlite3.Connection, directory: Path) -> None:
    migrations = load_migrations(directory)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS player_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        int(row["version"]): (str(row["name"]), str(row["checksum"]))
        for row in connection.execute(
            "SELECT version, name, checksum FROM player_schema_migrations ORDER BY version"
        )
    }
    for migration in migrations:
        previous = applied.get(migration.version)
        if previous is not None:
            if previous != (migration.name, migration.checksum):
                raise PlayerMigrationError(
                    f"player migration checksum mismatch at {migration.version:04d}"
                )
            continue
        # executescript starts by committing any pending transaction. Start the
        # migration transaction inside the script so the ledger insert below is
        # still part of the same atomic unit.
        connection.executescript("BEGIN IMMEDIATE;\n" + migration.sql)
        try:
            connection.execute(
                "INSERT INTO player_schema_migrations(version, name, checksum) VALUES(?,?,?)",
                (migration.version, migration.name, migration.checksum),
            )
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
