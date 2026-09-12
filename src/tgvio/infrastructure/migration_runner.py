from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from tgvio.infrastructure.schema import schema_identity, snapshot_diff


_MIGRATION_NAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
_LEDGER_SQL = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class MigrationError(RuntimeError):
    pass


class MigrationChecksumError(MigrationError):
    pass


class SchemaFingerprintError(MigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str
    path: Path


@dataclass(frozen=True)
class MigrationReport:
    latest_version: int
    recorded_versions: tuple[int, ...]
    applied_now: tuple[int, ...]
    user_version: int
    ledger_present: bool
    baseline_fingerprint: str
    backup_created: bool

    def safe_projection(self) -> dict[str, object]:
        return {
            "latest_version": self.latest_version,
            "recorded_versions": list(self.recorded_versions),
            "applied_now": list(self.applied_now),
            "user_version": self.user_version,
            "ledger_present": self.ledger_present,
            "baseline_fingerprint": self.baseline_fingerprint,
            "backup_created": self.backup_created,
        }


def _default_migrations_dir() -> Path:
    return Path(__file__).with_name("migrations")


def _load_migrations(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME_RE.match(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        raw = path.read_bytes()
        version = int(match.group("version"))
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                checksum=hashlib.sha256(raw).hexdigest(),
                sql=raw.decode("utf-8"),
                path=path,
            )
        )
    if not migrations:
        raise MigrationError("no migrations found")
    versions = [migration.version for migration in migrations]
    expected = list(range(1, len(migrations) + 1))
    if versions != expected:
        raise MigrationError(f"migration versions must be contiguous from 1: {versions}")
    return migrations


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _quick_check(connection: sqlite3.Connection) -> None:
    rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    if rows != ["ok"]:
        raise MigrationError("database quick_check failed")


def _split_sql_script(script: str) -> Iterable[str]:
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    if buffer.strip():
        raise MigrationError("migration contains an incomplete SQL statement")


def _execute_migration(connection: sqlite3.Connection, migration: Migration) -> None:
    for statement in _split_sql_script(migration.sql):
        connection.execute(statement)


def _baseline_identity(migration: Migration) -> tuple[str, dict[str, object]]:
    fixture = sqlite3.connect(":memory:", isolation_level=None)
    try:
        fixture.execute("PRAGMA foreign_keys=ON")
        fixture.execute("BEGIN IMMEDIATE")
        try:
            _execute_migration(fixture, migration)
        except Exception:
            fixture.rollback()
            raise
        else:
            fixture.commit()
        identity = schema_identity(fixture)
        return identity.fingerprint, identity.snapshot
    finally:
        fixture.close()


def _ledger_rows(connection: sqlite3.Connection) -> list[tuple[int, str, str]]:
    try:
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise MigrationError("migration ledger is unreadable") from exc
    return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]


def _validate_applied_migrations(
    rows: list[tuple[int, str, str]],
    migrations: list[Migration],
) -> None:
    if not rows:
        raise MigrationError("migration ledger exists but contains no baseline")
    expected_versions = list(range(1, len(rows) + 1))
    actual_versions = [row[0] for row in rows]
    if actual_versions != expected_versions:
        raise MigrationError("migration ledger versions are not contiguous")
    by_version = {migration.version: migration for migration in migrations}
    for version, name, checksum in rows:
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(f"database migration version {version} is newer than this code")
        if migration.name != name:
            raise MigrationChecksumError(f"migration {version:04d} name mismatch")
        if migration.checksum != checksum:
            raise MigrationChecksumError(f"migration {version:04d} checksum mismatch")


def _create_backup(
    connection: sqlite3.Connection,
    database_path: Path,
    backup_dir: Path,
    *,
    from_version: int,
    to_version: int,
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        backup_dir.chmod(0o700)
    except OSError:
        pass
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / (
        f"{database_path.stem}.pre-migrate-v{from_version}-to-v{to_version}-{timestamp}.sqlite3"
    )
    if destination.exists():
        raise MigrationError("migration backup destination already exists")
    target = sqlite3.connect(destination)
    try:
        connection.backup(target)
        _quick_check(target)
    finally:
        target.close()
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


class MigrationRunner:
    def __init__(
        self,
        database_path: Path,
        *,
        migrations_dir: Path | None = None,
        backup_dir: Path | None = None,
    ) -> None:
        self._database_path = database_path
        self._migrations_dir = migrations_dir or _default_migrations_dir()
        self._backup_dir = backup_dir or database_path.parent / "db_backups"

    def run(self) -> MigrationReport:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        migrations = _load_migrations(self._migrations_dir)
        baseline = migrations[0]
        existed_with_data = self._database_path.exists() and self._database_path.stat().st_size > 0
        connection = sqlite3.connect(self._database_path, timeout=30, isolation_level=None)
        backup_created = False
        applied_now: list[int] = []
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            _quick_check(connection)
            ledger_present = _table_exists(connection, "schema_migrations")
            if ledger_present:
                rows = _ledger_rows(connection)
                _validate_applied_migrations(rows, migrations)
                expected_fingerprint, expected_snapshot = _baseline_identity(baseline)
            else:
                expected_fingerprint, expected_snapshot = _baseline_identity(baseline)
                rows = []

            actual_identity = schema_identity(connection)
            schema_is_empty = not any(actual_identity.snapshot.get(key) for key in ("tables", "views", "triggers"))

            if not ledger_present:
                if schema_is_empty:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(_LEDGER_SQL)
                        _execute_migration(connection, baseline)
                        post_identity = schema_identity(connection)
                        if post_identity.fingerprint != expected_fingerprint:
                            raise SchemaFingerprintError("fresh baseline schema fingerprint mismatch")
                        connection.execute(
                            "INSERT INTO schema_migrations(version, name, checksum) VALUES(?,?,?)",
                            (baseline.version, baseline.name, baseline.checksum),
                        )
                        connection.execute(f"PRAGMA user_version={baseline.version}")
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                    applied_now.append(baseline.version)
                    ledger_present = True
                else:
                    if actual_identity.fingerprint != expected_fingerprint:
                        diff = snapshot_diff(expected_snapshot, actual_identity.snapshot)
                        raise SchemaFingerprintError(
                            "existing database does not match the TGVIO baseline: "
                            + json.dumps(diff, sort_keys=True, separators=(",", ":"))
                        )
                    if existed_with_data:
                        _create_backup(
                            connection,
                            self._database_path,
                            self._backup_dir,
                            from_version=0,
                            to_version=migrations[-1].version,
                        )
                        backup_created = True
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(_LEDGER_SQL)
                        connection.execute(
                            "INSERT INTO schema_migrations(version, name, checksum) VALUES(?,?,?)",
                            (baseline.version, baseline.name, baseline.checksum),
                        )
                        connection.execute(f"PRAGMA user_version={baseline.version}")
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                    applied_now.append(baseline.version)
                    ledger_present = True

            rows = _ledger_rows(connection)
            _validate_applied_migrations(rows, migrations)
            current_version = rows[-1][0]
            pending = [migration for migration in migrations if migration.version > current_version]
            if pending:
                if not backup_created and existed_with_data:
                    _create_backup(
                        connection,
                        self._database_path,
                        self._backup_dir,
                        from_version=current_version,
                        to_version=migrations[-1].version,
                    )
                    backup_created = True
                for migration in pending:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        _execute_migration(connection, migration)
                        connection.execute(
                            "INSERT INTO schema_migrations(version, name, checksum) VALUES(?,?,?)",
                            (migration.version, migration.name, migration.checksum),
                        )
                        connection.execute(f"PRAGMA user_version={migration.version}")
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                    applied_now.append(migration.version)
                rows = _ledger_rows(connection)
                _validate_applied_migrations(rows, migrations)

            _quick_check(connection)
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            recorded = tuple(row[0] for row in rows)
            latest_version = migrations[-1].version
            if recorded[-1] != latest_version:
                raise MigrationError("database did not reach the latest migration")
            if user_version != latest_version:
                raise MigrationError("PRAGMA user_version does not match migration ledger")
            return MigrationReport(
                latest_version=latest_version,
                recorded_versions=recorded,
                applied_now=tuple(applied_now),
                user_version=user_version,
                ledger_present=ledger_present,
                baseline_fingerprint=expected_fingerprint,
                backup_created=backup_created,
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
