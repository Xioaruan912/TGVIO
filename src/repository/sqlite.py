"""Async SQLite repository and forward-only migration runner.

R2-A deliberately does not replace the in-memory pipeline as the runtime source
of truth.  This module establishes the durable schema/lifecycle that later R2
work can dual-write into without coupling Telegram or WebDAV IO to database
transactions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

logger = logging.getLogger(__name__)


class RepositoryError(RuntimeError):
    """Base repository failure."""


class MigrationError(RepositoryError):
    """Migration discovery or execution failure."""


class MigrationChecksumError(MigrationError):
    """An already-applied migration no longer matches its recorded checksum."""


@dataclass(frozen=True)
class JobRecord:
    id: int
    kind: str
    user_id: int
    state: str
    source_kind: str
    revision: int
    accepted_at: float
    updated_at: float
    source_chat_id: int | None = None
    source_url: str | None = None
    status_chat_id: int | None = None
    status_message_id: int | None = None


@dataclass(frozen=True)
class JobEventRecord:
    id: int
    job_id: int
    event_type: str
    from_state: str | None
    to_state: str | None
    payload_json: str | None
    created_at: float


@dataclass(frozen=True)
class BackupAttemptRecord:
    id: int
    job_id: int
    state: str
    remote_dir: str
    retry_count: int
    next_retry_at: float | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class BackupFileRecord:
    id: int
    attempt_id: int
    local_path: str
    remote_name: str
    size_bytes: int
    state: str
    bytes_done: int


@dataclass(frozen=True)
class _Migration:
    version: int
    name: str
    path: Path
    checksum: str
    sql: str


class SQLiteRepository:
    REQUIRED_TABLES = frozenset(
        {
            "schema_migrations",
            "jobs",
            "job_events",
            "backup_attempts",
            "backup_files",
            "settings",
        }
    )

    def __init__(
        self,
        path: str | os.PathLike[str] = "session/state.sqlite3",
        *,
        migrations_dir: str | os.PathLike[str] | None = None,
        backup_dir: str | os.PathLike[str] | None = None,
        download_root: str | os.PathLike[str] = "downloads",
    ) -> None:
        self.path = Path(path)
        self.migrations_dir = (
            Path(migrations_dir)
            if migrations_dir is not None
            else Path(__file__).with_name("migrations")
        )
        self.backup_dir = (
            Path(backup_dir)
            if backup_dir is not None
            else self.path.parent / "db_backups"
        )
        self.download_root = Path(download_root).resolve()
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._preexisting = False
        self.last_backup_path: Path | None = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    async def open(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._preexisting = self.path.exists() and self.path.stat().st_size > 0
        try:
            conn = await aiosqlite.connect(self.path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA synchronous=FULL")
            self._conn = conn
        except Exception:
            logger.exception("Failed to open SQLite repository at %s", self.path)
            raise
        logger.debug(
            "SQLite repository open: python=%s sqlite=%s aiosqlite=%s",
            platform.python_version(),
            sqlite3.sqlite_version,
            getattr(aiosqlite, "__version__", "unknown"),
        )

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RepositoryError("repository is not open")
        return self._conn

    async def _ensure_migration_table(self) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version INTEGER PRIMARY KEY,
                  name TEXT NOT NULL,
                  applied_at REAL NOT NULL,
                  checksum TEXT NOT NULL
                )
                """
            )
            await conn.commit()

    @staticmethod
    def _split_sql(sql: str) -> Iterable[str]:
        buffer = ""
        for line in sql.splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                statement = buffer.strip()
                buffer = ""
                if statement:
                    yield statement
        if buffer.strip():
            raise MigrationError("migration contains an incomplete SQL statement")

    def _discover_migrations(self) -> list[_Migration]:
        if not self.migrations_dir.is_dir():
            raise MigrationError(f"migration directory missing: {self.migrations_dir}")
        migrations: list[_Migration] = []
        seen: set[int] = set()
        for path in sorted(self.migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version_text, _, name = path.name.partition("_")
            try:
                version = int(version_text)
            except ValueError as exc:
                raise MigrationError(f"invalid migration filename: {path.name}") from exc
            if version in seen:
                raise MigrationError(f"duplicate migration version: {version}")
            seen.add(version)
            raw = path.read_bytes()
            migrations.append(
                _Migration(
                    version=version,
                    name=name,
                    path=path,
                    checksum=hashlib.sha256(raw).hexdigest(),
                    sql=raw.decode("utf-8"),
                )
            )
        if not migrations:
            raise MigrationError("no migrations found")
        versions = [migration.version for migration in migrations]
        if versions != list(range(1, len(versions) + 1)):
            raise MigrationError(f"migration versions must be contiguous from 0001: {versions}")
        return migrations

    async def _migration_table_exists(self) -> bool:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def _applied_migrations(self) -> dict[int, tuple[str, str]]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {int(row["version"]): (str(row["name"]), str(row["checksum"])) for row in rows}

    async def _backup_before_migration(self) -> Path | None:
        if not self._preexisting:
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        target_path = self.backup_dir / f"state-pre-migrate-{stamp}.sqlite3"
        suffix = 1
        while target_path.exists():
            target_path = self.backup_dir / f"state-pre-migrate-{stamp}-{suffix}.sqlite3"
            suffix += 1
        target = sqlite3.connect(target_path)
        try:
            await self._require_conn().backup(target)
        finally:
            target.close()
        self.last_backup_path = target_path
        logger.info("SQLite pre-migration backup created: %s", target_path)
        return target_path

    async def migrate(self) -> list[int]:
        migrations = self._discover_migrations()
        applied = (
            await self._applied_migrations()
            if await self._migration_table_exists()
            else {}
        )
        available = {migration.version: migration for migration in migrations}

        unknown = sorted(set(applied) - set(available))
        if unknown:
            raise MigrationError(f"database has unknown migration versions: {unknown}")
        for version, (recorded_name, recorded_checksum) in applied.items():
            migration = available[version]
            if recorded_name != migration.name or recorded_checksum != migration.checksum:
                raise MigrationChecksumError(
                    f"migration {version:04d} checksum/name does not match applied database"
                )

        pending = [migration for migration in migrations if migration.version not in applied]
        if not pending:
            return []
        # Back up the untouched pre-migration database before any schema write.
        await self._backup_before_migration()
        await self._ensure_migration_table()

        conn = self._require_conn()
        applied_now: list[int] = []
        for migration in pending:
            async with self._write_lock:
                try:
                    await conn.execute("BEGIN IMMEDIATE")
                    for statement in self._split_sql(migration.sql):
                        await conn.execute(statement)
                    await conn.execute(
                        """
                        INSERT INTO schema_migrations(version, name, applied_at, checksum)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            migration.version,
                            migration.name,
                            time.time(),
                            migration.checksum,
                        ),
                    )
                    await conn.commit()
                except Exception as exc:
                    await conn.rollback()
                    raise MigrationError(
                        f"failed applying migration {migration.version:04d}_{migration.name}"
                    ) from exc
            applied_now.append(migration.version)
            logger.info("Applied SQLite migration %04d_%s", migration.version, migration.name)
        return applied_now

    async def self_check(self) -> dict[str, Any]:
        conn = self._require_conn()
        integrity_cursor = await conn.execute("PRAGMA integrity_check")
        integrity_row = await integrity_cursor.fetchone()
        await integrity_cursor.close()
        integrity = str(integrity_row[0]) if integrity_row else "missing"
        if integrity.lower() != "ok":
            raise RepositoryError(f"SQLite integrity_check failed: {integrity}")

        async def pragma_int(name: str) -> int:
            cursor = await conn.execute(f"PRAGMA {name}")
            row = await cursor.fetchone()
            await cursor.close()
            return int(row[0]) if row else -1

        foreign_keys = await pragma_int("foreign_keys")
        busy_timeout = await pragma_int("busy_timeout")
        synchronous = await pragma_int("synchronous")
        if foreign_keys != 1 or busy_timeout < 5000 or synchronous != 2:
            raise RepositoryError(
                "SQLite pragma self-check failed "
                f"(foreign_keys={foreign_keys}, busy_timeout={busy_timeout}, synchronous={synchronous})"
            )

        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = {str(row[0]) for row in await cursor.fetchall()}
        await cursor.close()
        missing = self.REQUIRED_TABLES - tables
        if missing:
            raise RepositoryError(f"SQLite schema missing tables: {sorted(missing)}")
        return {
            "integrity": integrity,
            "foreign_keys": foreign_keys,
            "busy_timeout": busy_timeout,
            "synchronous": synchronous,
            "tables": frozenset(tables),
        }

    async def schema_versions(self) -> list[int]:
        await self._ensure_migration_table()
        applied = await self._applied_migrations()
        return sorted(applied)

    async def create_job(
        self,
        *,
        kind: str,
        user_id: int,
        state: str,
        source_kind: str,
        source_chat_id: int | None = None,
        source_url: str | None = None,
        status_chat_id: int | None = None,
        status_message_id: int | None = None,
        spoiler: bool = False,
        event_type: str = "accepted",
        event_payload: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> JobRecord:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        payload_json = self._encode_versioned_payload(event_payload)
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """
                    INSERT INTO jobs(
                      kind, user_id, state, spoiler, source_kind, source_chat_id,
                      source_url, status_chat_id, status_message_id, accepted_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        user_id,
                        state,
                        int(spoiler),
                        source_kind,
                        source_chat_id,
                        source_url,
                        status_chat_id,
                        status_message_id,
                        timestamp,
                        timestamp,
                    ),
                )
                job_id = int(cursor.lastrowid)
                await cursor.close()
                await conn.execute(
                    """
                    INSERT INTO job_events(job_id, event_type, from_state, to_state, payload_json, created_at)
                    VALUES (?, ?, NULL, ?, ?, ?)
                    """,
                    (job_id, event_type, state, payload_json, timestamp),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        record = await self.get_job(job_id)
        if record is None:
            raise RepositoryError(f"created job {job_id} could not be read back")
        return record

    async def get_job(self, job_id: int) -> JobRecord | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT id, kind, user_id, state, source_kind, revision, accepted_at, updated_at,
                   source_chat_id, source_url, status_chat_id, status_message_id
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._job_from_row(row) if row is not None else None

    async def list_jobs(self, *, user_id: int | None = None, limit: int = 100) -> list[JobRecord]:
        conn = self._require_conn()
        limit = max(1, min(int(limit), 500))
        sql = (
            "SELECT id, kind, user_id, state, source_kind, revision, accepted_at, updated_at, "
            "source_chat_id, source_url, status_chat_id, status_message_id FROM jobs"
        )
        params: tuple[Any, ...]
        if user_id is None:
            sql += " ORDER BY id LIMIT ?"
            params = (limit,)
        else:
            sql += " WHERE user_id = ? ORDER BY id LIMIT ?"
            params = (user_id, limit)
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._job_from_row(row) for row in rows]

    @staticmethod
    def _job_from_row(row: aiosqlite.Row) -> JobRecord:
        return JobRecord(
            id=int(row["id"]),
            kind=str(row["kind"]),
            user_id=int(row["user_id"]),
            state=str(row["state"]),
            source_kind=str(row["source_kind"]),
            revision=int(row["revision"]),
            accepted_at=float(row["accepted_at"]),
            updated_at=float(row["updated_at"]),
            source_chat_id=row["source_chat_id"],
            source_url=row["source_url"],
            status_chat_id=row["status_chat_id"],
            status_message_id=row["status_message_id"],
        )

    async def list_job_events(self, job_id: int) -> list[JobEventRecord]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT id, job_id, event_type, from_state, to_state, payload_json, created_at
            FROM job_events WHERE job_id = ? ORDER BY id
            """,
            (job_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            JobEventRecord(
                id=int(row["id"]),
                job_id=int(row["job_id"]),
                event_type=str(row["event_type"]),
                from_state=row["from_state"],
                to_state=row["to_state"],
                payload_json=row["payload_json"],
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    async def create_backup_attempt(
        self,
        *,
        job_id: int,
        state: str,
        remote_dir: str,
        now: float | None = None,
    ) -> BackupAttemptRecord:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """
                    INSERT INTO backup_attempts(job_id, state, remote_dir, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (job_id, state, remote_dir, timestamp, timestamp),
                )
                attempt_id = int(cursor.lastrowid)
                await cursor.close()
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        cursor = await conn.execute(
            """
            SELECT id, job_id, state, remote_dir, retry_count, next_retry_at, created_at, updated_at
            FROM backup_attempts WHERE id = ?
            """,
            (attempt_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RepositoryError(f"created backup attempt {attempt_id} could not be read back")
        return BackupAttemptRecord(
            id=int(row["id"]),
            job_id=int(row["job_id"]),
            state=str(row["state"]),
            remote_dir=str(row["remote_dir"]),
            retry_count=int(row["retry_count"]),
            next_retry_at=row["next_retry_at"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    async def create_backup_file(
        self,
        *,
        attempt_id: int,
        local_path: str,
        remote_name: str,
        size_bytes: int,
        state: str = "pending",
    ) -> BackupFileRecord:
        conn = self._require_conn()
        local_path = self._validated_local_path(local_path)
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """
                    INSERT INTO backup_files(attempt_id, local_path, remote_name, size_bytes, state)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (attempt_id, local_path, remote_name, int(size_bytes), state),
                )
                file_id = int(cursor.lastrowid)
                await cursor.close()
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        cursor = await conn.execute(
            """
            SELECT id, attempt_id, local_path, remote_name, size_bytes, state, bytes_done
            FROM backup_files WHERE id = ?
            """,
            (file_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RepositoryError(f"created backup file {file_id} could not be read back")
        return BackupFileRecord(
            id=int(row["id"]),
            attempt_id=int(row["attempt_id"]),
            local_path=str(row["local_path"]),
            remote_name=str(row["remote_name"]),
            size_bytes=int(row["size_bytes"]),
            state=str(row["state"]),
            bytes_done=int(row["bytes_done"]),
        )

    @staticmethod
    def _encode_versioned_payload(payload: dict[str, Any] | None) -> str | None:
        if payload is None:
            return None
        version = payload.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise RepositoryError("payload_json requires integer schema_version >= 1")
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _validated_local_path(self, local_path: str) -> str:
        resolved = Path(local_path).resolve()
        try:
            common = Path(os.path.commonpath((self.download_root, resolved)))
        except ValueError as exc:
            raise RepositoryError("backup local_path is outside download root") from exc
        if common != self.download_root:
            raise RepositoryError("backup local_path is outside download root")
        return str(resolved)

    async def set_setting(self, key: str, value: Any, *, scope: str = "global") -> int:
        conn = self._require_conn()
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        timestamp = time.time()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                await conn.execute(
                    """
                    INSERT INTO settings(scope, key, value_json, revision, updated_at)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(scope, key) DO UPDATE SET
                      value_json=excluded.value_json,
                      revision=settings.revision + 1,
                      updated_at=excluded.updated_at
                    """,
                    (scope, key, encoded, timestamp),
                )
                cursor = await conn.execute(
                    "SELECT revision FROM settings WHERE scope = ? AND key = ?",
                    (scope, key),
                )
                row = await cursor.fetchone()
                await cursor.close()
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return int(row[0]) if row else 0

    async def get_setting(self, key: str, *, scope: str = "global", default: Any = None) -> Any:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT value_json FROM settings WHERE scope = ? AND key = ?",
            (scope, key),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return default
        return json.loads(str(row[0]))
