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
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from ..state_machine import InvalidTransition, plan_transition

logger = logging.getLogger(__name__)


def _sanitize_error_message(value: object) -> str:
    text = str(value or "")[:1000]
    text = re.sub(r"https?://[^\s/@:]+:[^\s/@]+@", "https://***@", text)
    text = re.sub(r"(?i)(authorization|token|password|passwd)\s*[:=]\s*[^\s]+", r"\1=***", text)
    return text


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
    resume_state: str | None = None
    legacy_seq: int | None = None
    spoiler: bool = False


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
class BackupFileStatusRecord:
    id: int
    attempt_id: int
    local_path: str
    remote_name: str
    size_bytes: int
    state: str
    bytes_done: int
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class JobItemRecord:
    id: int
    job_id: int
    ordinal: int
    source_chat_id: int | None
    source_message_id: int | None
    grouped_id: int | None
    media_kind: str | None
    local_path: str | None
    size_bytes: int
    metadata_json: str | None


@dataclass(frozen=True)
class PublishedMessageRecord:
    id: int
    job_id: int
    peer_id: int
    message_id: int
    role: str
    created_at: float
    deleted_at: float | None


@dataclass(frozen=True)
class InteractionSessionRecord:
    user_id: int
    kind: str
    field: str | None
    payload_json: str | None
    revision: int
    expires_at: float
    updated_at: float


@dataclass(frozen=True)
class TransitionResult:
    applied: bool
    job: JobRecord


@dataclass(frozen=True)
class JobClaim:
    job: JobRecord
    owner: str
    kind: str
    heartbeat_at: float


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
            "job_items",
            "job_texts",
            "published_messages",
            "interaction_sessions",
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

    async def _table_has_column(self, table: str, column: str) -> bool:
        conn = self._require_conn()
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        await cursor.close()
        return any(str(row[1]) == column for row in rows)

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

    async def accept_job(
        self,
        *,
        kind: str,
        user_id: int,
        state: str,
        source_kind: str,
        items: list[dict[str, Any]] | None = None,
        texts: list[str] | None = None,
        source_chat_id: int | None = None,
        source_url: str | None = None,
        legacy_seq: int | None = None,
        spoiler: bool = False,
        event_type: str = "accepted",
        event_payload: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> JobRecord:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        payload_json = self._encode_versioned_payload(event_payload)
        prepared = []
        for ordinal, item in enumerate(items or [], start=1):
            metadata_json = self._encode_versioned_payload(item.get("metadata"))
            local_path = item.get("local_path")
            if local_path:
                local_path = self._validated_local_path(str(local_path))
            prepared.append((ordinal, item, local_path, metadata_json))
        has_legacy_seq = await self._table_has_column("jobs", "legacy_seq")
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                if has_legacy_seq:
                    cursor = await conn.execute(
                        """
                        INSERT INTO jobs(
                          kind, user_id, state, spoiler, source_kind, source_chat_id,
                          source_url, legacy_seq, total_items, accepted_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (kind, user_id, state, int(spoiler), source_kind, source_chat_id,
                         source_url, legacy_seq, len(prepared), timestamp, timestamp),
                    )
                else:
                    cursor = await conn.execute(
                        """
                        INSERT INTO jobs(
                          kind, user_id, state, spoiler, source_kind, source_chat_id,
                          source_url, total_items, accepted_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (kind, user_id, state, int(spoiler), source_kind, source_chat_id,
                         source_url, len(prepared), timestamp, timestamp),
                    )
                job_id = int(cursor.lastrowid)
                await cursor.close()
                for ordinal, item, local_path, metadata_json in prepared:
                    await conn.execute(
                        """
                        INSERT INTO job_items(
                          job_id, ordinal, source_chat_id, source_message_id, grouped_id,
                          media_kind, original_name, mime_type, local_path, size_bytes,
                          download_state, publish_state, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (job_id, ordinal, item.get("source_chat_id"), item.get("source_message_id"),
                         item.get("grouped_id"), item.get("media_kind"), item.get("original_name"),
                         item.get("mime_type"), local_path, int(item.get("size_bytes") or 0),
                         item.get("download_state") or "pending", item.get("publish_state") or "pending",
                         metadata_json),
                    )
                for ordinal, text in enumerate(texts or [], start=1):
                    await conn.execute(
                        "INSERT INTO job_texts(job_id, ordinal, text) VALUES (?, ?, ?)",
                        (job_id, ordinal, str(text)),
                    )
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
            raise RepositoryError(f"accepted job {job_id} could not be read back")
        return record

    async def record_job_event(
        self,
        job_id: int,
        event_type: str,
        *,
        to_state: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn = self._require_conn()
        timestamp = time.time()
        payload_json = self._encode_versioned_payload(payload)
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,))
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    raise RepositoryError(f"job {job_id} not found")
                from_state = str(row[0])
                if to_state is not None:
                    await conn.execute(
                        "UPDATE jobs SET state=?, updated_at=?, revision=revision+1 WHERE id=?",
                        (to_state, timestamp, job_id),
                    )
                await conn.execute(
                    "INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (job_id, event_type, from_state, to_state, payload_json, timestamp),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def transition_job(
        self,
        job_id: int,
        *,
        expected_revision: int,
        to_state: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> TransitionResult:
        """Atomically apply one state-machine transition using revision CAS.

        A stale expected_revision returns applied=False without writing an event.
        """
        conn = self._require_conn()
        timestamp = time.time()
        payload_json = self._encode_versioned_payload(payload)
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT state,resume_state,revision FROM jobs WHERE id=?",
                    (job_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    raise RepositoryError(f"job {job_id} not found")
                current_revision = int(row["revision"])
                if current_revision != int(expected_revision):
                    await conn.rollback()
                    current = await self.get_job(job_id)
                    if current is None:
                        raise RepositoryError(f"job {job_id} not found after stale transition")
                    return TransitionResult(False, current)

                plan = plan_transition(
                    str(row["state"]),
                    to_state,
                    current_resume_state=row["resume_state"],
                )
                error_message = None
                error_code = None
                retry_count = None
                next_retry_at = None
                if plan.to_state == "failed" and payload:
                    error_message = _sanitize_error_message(
                        payload.get("error_message") or payload.get("message") or payload.get("reason")
                    )
                    error_code = str(payload.get("error_code") or "unknown")[:64]
                    retry_count = max(0, int(payload.get("retry_count") or 0))
                    next_retry_at = payload.get("next_retry_at")
                cursor = await conn.execute(
                    """
                    UPDATE jobs
                    SET state=?, resume_state=?, updated_at=?, revision=revision+1,
                        claim_owner=NULL, claim_kind=NULL, heartbeat_at=NULL,
                        error_code=?,
                        error_message=?,
                        retry_count=COALESCE(?,retry_count),
                        next_retry_at=?
                    WHERE id=? AND revision=?
                    """,
                    (
                        plan.to_state,
                        plan.resume_state,
                        timestamp,
                        error_code,
                        error_message or None,
                        retry_count,
                        next_retry_at,
                        job_id,
                        int(expected_revision),
                    ),
                )
                changed = cursor.rowcount == 1
                await cursor.close()
                if not changed:
                    await conn.rollback()
                    current = await self.get_job(job_id)
                    if current is None:
                        raise RepositoryError(f"job {job_id} missing after CAS miss")
                    return TransitionResult(False, current)
                await conn.execute(
                    """
                    INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        job_id,
                        event_type,
                        plan.from_state,
                        plan.to_state,
                        payload_json,
                        timestamp,
                    ),
                )
                await conn.commit()
            except InvalidTransition:
                await conn.rollback()
                raise
            except Exception:
                await conn.rollback()
                raise
        current = await self.get_job(job_id)
        if current is None:
            raise RepositoryError(f"job {job_id} missing after transition")
        return TransitionResult(True, current)

    async def record_job_retry(
        self,
        job_id: int,
        *,
        phase: str,
        error_code: str,
        error_message: str,
        retry_count: int,
        next_retry_at: float,
    ) -> None:
        """Persist a non-terminal failed attempt without releasing its claim."""
        conn = self._require_conn()
        timestamp = time.time()
        safe_code = str(error_code or "unknown")[:64]
        safe_message = _sanitize_error_message(error_message)
        payload_json = self._encode_versioned_payload(
            {
                "schema_version": 1,
                "phase": str(phase),
                "error_code": safe_code,
                "error_message": safe_message,
                "retry_count": max(0, int(retry_count)),
                "next_retry_at": float(next_retry_at),
            }
        )
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute("SELECT state FROM jobs WHERE id=?", (int(job_id),))
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    raise RepositoryError(f"job {job_id} not found")
                await conn.execute(
                    """UPDATE jobs
                       SET error_code=?,error_message=?,retry_count=?,next_retry_at=?,updated_at=?
                       WHERE id=?""",
                    (safe_code, safe_message, max(0, int(retry_count)), float(next_retry_at), timestamp, int(job_id)),
                )
                await conn.execute(
                    """INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (int(job_id), f"{phase}_retry_scheduled", str(row["state"]), str(row["state"]), payload_json, timestamp),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def _claim_job(self, *, kind: str, owner: str) -> JobClaim | None:
        if kind not in {"download", "publish"}:
            raise RepositoryError(f"unknown claim kind: {kind}")
        conn = self._require_conn()
        timestamp = time.time()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                if kind == "download":
                    cursor = await conn.execute(
                        """
                        SELECT id,state,resume_state,revision FROM jobs
                        WHERE state='queued' AND claim_owner IS NULL
                        ORDER BY id LIMIT 1
                        """
                    )
                    target_state = "downloading"
                    substate_sql = "download_state='running'"
                else:
                    cursor = await conn.execute(
                        """
                        SELECT j.id,j.state,j.resume_state,j.revision FROM jobs j
                        WHERE j.state='ready' AND j.claim_owner IS NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM jobs earlier
                            WHERE earlier.id < j.id
                              AND earlier.state IN ('queued','downloading','ready','publishing')
                          )
                        ORDER BY j.id LIMIT 1
                        """
                    )
                    target_state = "publishing"
                    substate_sql = "publish_state='running'"
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    await conn.rollback()
                    return None
                plan = plan_transition(
                    str(row["state"]), target_state, current_resume_state=row["resume_state"]
                )
                job_id = int(row["id"])
                revision = int(row["revision"])
                cursor = await conn.execute(
                    f"""
                    UPDATE jobs
                    SET state=?, {substate_sql}, claim_owner=?, claim_kind=?, heartbeat_at=?,
                        updated_at=?, started_at=COALESCE(started_at,?), revision=revision+1
                    WHERE id=? AND revision=? AND claim_owner IS NULL
                    """,
                    (
                        plan.to_state,
                        owner,
                        kind,
                        timestamp,
                        timestamp,
                        timestamp,
                        job_id,
                        revision,
                    ),
                )
                changed = cursor.rowcount == 1
                await cursor.close()
                if not changed:
                    await conn.rollback()
                    return None
                await conn.execute(
                    """
                    INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        job_id,
                        f"{kind}_claimed",
                        plan.from_state,
                        plan.to_state,
                        self._encode_versioned_payload(
                            {"schema_version": 1, "owner": owner}
                        ),
                        timestamp,
                    ),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        current = await self.get_job(job_id)
        if current is None:
            raise RepositoryError(f"job {job_id} missing after claim")
        return JobClaim(current, owner, kind, timestamp)

    async def claim_next_download(self, owner: str) -> JobClaim | None:
        return await self._claim_job(kind="download", owner=owner)

    async def claim_next_publish(self, owner: str) -> JobClaim | None:
        return await self._claim_job(kind="publish", owner=owner)

    async def heartbeat_claim(self, job_id: int, *, owner: str, kind: str) -> bool:
        conn = self._require_conn()
        timestamp = time.time()
        async with self._write_lock:
            cursor = await conn.execute(
                """
                UPDATE jobs SET heartbeat_at=?, updated_at=?
                WHERE id=? AND claim_owner=? AND claim_kind=?
                """,
                (timestamp, timestamp, int(job_id), owner, kind),
            )
            changed = cursor.rowcount == 1
            await cursor.close()
            await conn.commit()
        return changed

    async def interrupt_claim(
        self,
        job_id: int,
        *,
        owner: str,
        kind: str,
        reason: str = "shutdown",
    ) -> TransitionResult:
        """Atomically move an owned active claim to interrupted."""
        conn = self._require_conn()
        timestamp = time.time()
        payload_json = self._encode_versioned_payload(
            {"schema_version": 1, "reason": reason, "owner": owner, "kind": kind}
        )
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT state,resume_state,revision,claim_owner,claim_kind FROM jobs WHERE id=?",
                    (job_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    raise RepositoryError(f"job {job_id} not found")
                if row["claim_owner"] != owner or row["claim_kind"] != kind:
                    await conn.rollback()
                    current = await self.get_job(job_id)
                    if current is None:
                        raise RepositoryError(f"job {job_id} missing after claim mismatch")
                    return TransitionResult(False, current)
                plan = plan_transition(
                    str(row["state"]),
                    "interrupted",
                    current_resume_state=row["resume_state"],
                )
                cursor = await conn.execute(
                    """
                    UPDATE jobs
                    SET state='interrupted', resume_state=NULL, claim_owner=NULL,
                        claim_kind=NULL, heartbeat_at=NULL, updated_at=?, revision=revision+1
                    WHERE id=? AND revision=? AND claim_owner=? AND claim_kind=?
                    """,
                    (timestamp, job_id, int(row["revision"]), owner, kind),
                )
                changed = cursor.rowcount == 1
                await cursor.close()
                if not changed:
                    await conn.rollback()
                    current = await self.get_job(job_id)
                    if current is None:
                        raise RepositoryError(f"job {job_id} missing after interrupt CAS miss")
                    return TransitionResult(False, current)
                await conn.execute(
                    """
                    INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (job_id, "shutdown_interrupted", plan.from_state, "interrupted", payload_json, timestamp),
                )
                await conn.commit()
            except InvalidTransition:
                await conn.rollback()
                raise
            except Exception:
                await conn.rollback()
                raise
        current = await self.get_job(job_id)
        if current is None:
            raise RepositoryError(f"job {job_id} missing after interrupt")
        return TransitionResult(True, current)

    async def record_download_ready(
        self,
        job_id: int,
        paths: list[str],
        *,
        expected_revision: int,
    ) -> TransitionResult:
        conn = self._require_conn()
        resolved = [self._validated_local_path(path) for path in paths]
        timestamp = time.time()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT state,resume_state,revision FROM jobs WHERE id=?", (job_id,)
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    raise RepositoryError(f"job {job_id} not found")
                if int(row["revision"]) != int(expected_revision):
                    await conn.rollback()
                    current = await self.get_job(job_id)
                    if current is None:
                        raise RepositoryError(f"job {job_id} missing after stale download completion")
                    return TransitionResult(False, current)
                plan = plan_transition(
                    str(row["state"]), "ready", current_resume_state=row["resume_state"]
                )
                cursor = await conn.execute(
                    "SELECT id,ordinal FROM job_items WHERE job_id=? ORDER BY ordinal", (job_id,)
                )
                items = await cursor.fetchall()
                await cursor.close()
                if items and len(items) != len(resolved):
                    raise RepositoryError(
                        f"download path count mismatch for job {job_id}: {len(resolved)} != {len(items)}"
                    )
                if not items:
                    for ordinal, path in enumerate(resolved, start=1):
                        await conn.execute(
                            """
                            INSERT INTO job_items(job_id,ordinal,local_path,size_bytes,download_state,publish_state,metadata_json)
                            VALUES (?,?,?,?,?,?,?)
                            """,
                            (
                                job_id,
                                ordinal,
                                path,
                                os.path.getsize(path),
                                "succeeded",
                                "pending",
                                self._encode_versioned_payload({"schema_version": 1, "recovered_descriptor": "local_path"}),
                            ),
                        )
                else:
                    for item, path in zip(items, resolved):
                        await conn.execute(
                            """
                            UPDATE job_items
                            SET local_path=?, size_bytes=?, download_state='succeeded'
                            WHERE id=?
                            """,
                            (path, os.path.getsize(path), int(item["id"])),
                        )
                local_dir = str(Path(resolved[0]).parent) if resolved else None
                cursor = await conn.execute(
                    """
                    UPDATE jobs
                    SET state='ready', download_state='succeeded', local_dir=?, updated_at=?,
                        revision=revision+1, claim_owner=NULL, claim_kind=NULL, heartbeat_at=NULL
                    WHERE id=? AND revision=?
                    """,
                    (local_dir, timestamp, job_id, int(expected_revision)),
                )
                if cursor.rowcount != 1:
                    await cursor.close()
                    await conn.rollback()
                    current = await self.get_job(job_id)
                    if current is None:
                        raise RepositoryError(f"job {job_id} missing after download CAS miss")
                    return TransitionResult(False, current)
                await cursor.close()
                await conn.execute(
                    "INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (
                        job_id,
                        "download_completed",
                        plan.from_state,
                        "ready",
                        self._encode_versioned_payload({"schema_version": 1, "count": len(resolved)}),
                        timestamp,
                    ),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        current = await self.get_job(job_id)
        if current is None:
            raise RepositoryError(f"job {job_id} missing after download completion")
        return TransitionResult(True, current)

    async def list_incomplete_jobs(self) -> list[JobRecord]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT id,kind,user_id,state,source_kind,revision,accepted_at,updated_at,resume_state,
                   source_chat_id,source_url,status_chat_id,status_message_id,legacy_seq,spoiler
            FROM jobs
            WHERE state IN ('queued','downloading','ready','publishing','interrupted')
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._job_from_row(row) for row in rows]

    async def list_job_items(self, job_id: int) -> list[JobItemRecord]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id,job_id,ordinal,source_chat_id,source_message_id,grouped_id,media_kind,local_path,size_bytes,metadata_json FROM job_items WHERE job_id=? ORDER BY ordinal",
            (job_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [JobItemRecord(int(r["id"]), int(r["job_id"]), int(r["ordinal"]), r["source_chat_id"], r["source_message_id"], r["grouped_id"], r["media_kind"], r["local_path"], int(r["size_bytes"]), r["metadata_json"]) for r in rows]

    async def append_job_items(self, job_id: int, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        conn = self._require_conn()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT COALESCE(MAX(ordinal),0) FROM job_items WHERE job_id=?",
                    (job_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                base = int(row[0])
                for offset, item in enumerate(items, start=1):
                    metadata_json = self._encode_versioned_payload(item.get("metadata"))
                    local_path = item.get("local_path")
                    if local_path:
                        local_path = self._validated_local_path(str(local_path))
                    await conn.execute(
                        """
                        INSERT INTO job_items(
                          job_id, ordinal, source_chat_id, source_message_id, grouped_id,
                          media_kind, original_name, mime_type, local_path, size_bytes,
                          download_state, publish_state, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (job_id, base + offset, item.get("source_chat_id"), item.get("source_message_id"),
                         item.get("grouped_id"), item.get("media_kind"), item.get("original_name"),
                         item.get("mime_type"), local_path, int(item.get("size_bytes") or 0),
                         item.get("download_state") or "pending", item.get("publish_state") or "pending",
                         metadata_json),
                    )
                await conn.execute(
                    "UPDATE jobs SET total_items=total_items+?, updated_at=?, revision=revision+1 WHERE id=?",
                    (len(items), time.time(), job_id),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def list_backup_attempts(self, job_id: int) -> list[BackupAttemptRecord]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id,job_id,state,remote_dir,retry_count,next_retry_at,created_at,updated_at FROM backup_attempts WHERE job_id=? ORDER BY id",
            (job_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [BackupAttemptRecord(int(r["id"]), int(r["job_id"]), str(r["state"]), str(r["remote_dir"]), int(r["retry_count"]), r["next_retry_at"], float(r["created_at"]), float(r["updated_at"])) for r in rows]

    async def list_backup_files(self, attempt_id: int) -> list[BackupFileRecord]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id,attempt_id,local_path,remote_name,size_bytes,state,bytes_done FROM backup_files WHERE attempt_id=? ORDER BY id",
            (attempt_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [BackupFileRecord(int(r["id"]), int(r["attempt_id"]), str(r["local_path"]), str(r["remote_name"]), int(r["size_bytes"]), str(r["state"]), int(r["bytes_done"])) for r in rows]

    async def list_job_texts(self, job_id: int) -> list[str]:
        conn = self._require_conn()
        cursor = await conn.execute("SELECT text FROM job_texts WHERE job_id=? ORDER BY ordinal", (job_id,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [str(r[0]) for r in rows]

    async def record_published_messages(
        self,
        job_id: int,
        messages: list[tuple[int, int, str]],
        *,
        expected_revision: int,
    ) -> TransitionResult:
        conn = self._require_conn()
        timestamp = time.time()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT state,resume_state,revision FROM jobs WHERE id=?", (job_id,)
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    raise RepositoryError(f"job {job_id} not found")
                if int(row["revision"]) != int(expected_revision):
                    await conn.rollback()
                    current = await self.get_job(job_id)
                    if current is None:
                        raise RepositoryError(f"job {job_id} missing after stale publish")
                    return TransitionResult(False, current)
                plan = plan_transition(
                    str(row["state"]),
                    "succeeded",
                    current_resume_state=row["resume_state"],
                )
                for peer_id, message_id, role in messages:
                    await conn.execute(
                        "INSERT OR IGNORE INTO published_messages(job_id,peer_id,message_id,role,created_at) VALUES (?,?,?,?,?)",
                        (job_id, int(peer_id), int(message_id), str(role), timestamp),
                    )
                cursor = await conn.execute(
                    """
                    UPDATE jobs
                    SET state='succeeded', publish_state='succeeded', updated_at=?, finished_at=?,
                        revision=revision+1, claim_owner=NULL, claim_kind=NULL, heartbeat_at=NULL
                    WHERE id=? AND revision=?
                    """,
                    (timestamp, timestamp, job_id, int(expected_revision)),
                )
                if cursor.rowcount != 1:
                    await cursor.close()
                    await conn.rollback()
                    current = await self.get_job(job_id)
                    if current is None:
                        raise RepositoryError(f"job {job_id} missing after publish CAS miss")
                    return TransitionResult(False, current)
                await cursor.close()
                await conn.execute(
                    "INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (job_id, "published", plan.from_state, "succeeded", self._encode_versioned_payload({"schema_version": 1, "count": len(messages)}), timestamp),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        current = await self.get_job(job_id)
        if current is None:
            raise RepositoryError(f"job {job_id} missing after publish")
        return TransitionResult(True, current)

    async def checkpoint_published_messages(
        self,
        job_id: int,
        messages: list[tuple[int, int, str]],
    ) -> int:
        """Durably record confirmed Telegram side effects without finishing the job."""
        if not messages:
            return 0
        conn = self._require_conn()
        timestamp = time.time()
        inserted = 0
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute("SELECT state FROM jobs WHERE id=?", (int(job_id),))
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    raise RepositoryError(f"job {job_id} not found")
                for peer_id, message_id, role in messages:
                    cursor = await conn.execute(
                        """INSERT OR IGNORE INTO published_messages(
                               job_id,peer_id,message_id,role,created_at
                           ) VALUES (?,?,?,?,?)""",
                        (int(job_id), int(peer_id), int(message_id), str(role), timestamp),
                    )
                    inserted += max(0, int(cursor.rowcount))
                    await cursor.close()
                if inserted:
                    await conn.execute(
                        """INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (
                            int(job_id),
                            "publish_checkpoint",
                            str(row["state"]),
                            str(row["state"]),
                            self._encode_versioned_payload(
                                {"schema_version": 1, "received": len(messages), "inserted": inserted}
                            ),
                            timestamp,
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return inserted

    async def list_published_messages(self, job_id: int) -> list[PublishedMessageRecord]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id,job_id,peer_id,message_id,role,created_at,deleted_at FROM published_messages WHERE job_id=? ORDER BY id",
            (job_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [PublishedMessageRecord(int(r["id"]), int(r["job_id"]), int(r["peer_id"]), int(r["message_id"]), str(r["role"]), float(r["created_at"]), r["deleted_at"]) for r in rows]

    async def mark_published_deleted(
        self,
        job_id: int,
        *,
        expected_revision: int,
        message_ids: list[int],
    ) -> bool:
        conn = self._require_conn()
        timestamp = time.time()
        ids = sorted({int(value) for value in message_ids})
        if not ids:
            return False
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT revision FROM jobs WHERE id=?", (int(job_id),)
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None or int(row["revision"]) != int(expected_revision):
                    await conn.rollback()
                    return False
                marks = ",".join("?" for _ in ids)
                await conn.execute(
                    f"UPDATE published_messages SET deleted_at=? WHERE job_id=? AND id IN ({marks}) AND deleted_at IS NULL",
                    (timestamp, int(job_id), *ids),
                )
                cursor = await conn.execute(
                    "UPDATE jobs SET revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                    (timestamp, int(job_id), int(expected_revision)),
                )
                changed = cursor.rowcount == 1
                await cursor.close()
                if not changed:
                    await conn.rollback()
                    return False
                await conn.execute(
                    "INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at) "
                    "SELECT id,'published_messages_deleted',state,state,?,? FROM jobs WHERE id=?",
                    (
                        self._encode_versioned_payload(
                            {"schema_version": 1, "count": len(ids)}
                        ),
                        timestamp,
                        int(job_id),
                    ),
                )
                await conn.commit()
                return True
            except Exception:
                await conn.rollback()
                raise

    async def clear_failed_cache(self, job_id: int, *, expected_revision: int) -> bool:
        """Clear local cache references for a failed job using revision CAS."""
        conn = self._require_conn()
        timestamp = time.time()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT state,revision FROM jobs WHERE id=?", (int(job_id),)
                )
                row = await cursor.fetchone()
                await cursor.close()
                if (
                    row is None
                    or str(row["state"]) != "failed"
                    or int(row["revision"]) != int(expected_revision)
                ):
                    await conn.rollback()
                    return False
                await conn.execute(
                    "UPDATE job_items SET local_path=NULL WHERE job_id=?",
                    (int(job_id),),
                )
                cursor = await conn.execute(
                    "UPDATE jobs SET local_dir=NULL,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                    (timestamp, int(job_id), int(expected_revision)),
                )
                changed = cursor.rowcount == 1
                await cursor.close()
                if not changed:
                    await conn.rollback()
                    return False
                await conn.execute(
                    "INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at) "
                    "VALUES (?,'cache_deleted','failed','failed',?,?)",
                    (
                        int(job_id),
                        self._encode_versioned_payload({"schema_version": 1}),
                        timestamp,
                    ),
                )
                await conn.commit()
                return True
            except Exception:
                await conn.rollback()
                raise

    async def upsert_interaction_session(self, *, user_id: int, kind: str, field: str | None, payload: dict[str, Any] | None, revision: int, expires_at: float) -> InteractionSessionRecord:
        conn = self._require_conn()
        timestamp = time.time()
        payload_json = self._encode_versioned_payload(payload)
        async with self._write_lock:
            await conn.execute(
                """
                INSERT INTO interaction_sessions(user_id,kind,field,payload_json,revision,expires_at,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET kind=excluded.kind,field=excluded.field,payload_json=excluded.payload_json,revision=excluded.revision,expires_at=excluded.expires_at,updated_at=excluded.updated_at
                """,
                (user_id, kind, field, payload_json, int(revision), float(expires_at), timestamp),
            )
            await conn.commit()
        return InteractionSessionRecord(user_id, kind, field, payload_json, int(revision), float(expires_at), timestamp)

    async def delete_interaction_session(self, user_id: int, *, revision: int | None = None) -> bool:
        conn = self._require_conn()
        async with self._write_lock:
            if revision is None:
                cursor = await conn.execute("DELETE FROM interaction_sessions WHERE user_id=?", (user_id,))
            else:
                cursor = await conn.execute("DELETE FROM interaction_sessions WHERE user_id=? AND revision=?", (user_id, int(revision)))
            changed = cursor.rowcount > 0
            await cursor.close()
            await conn.commit()
        return changed

    async def get_job(self, job_id: int) -> JobRecord | None:
        conn = self._require_conn()
        legacy_expr = "legacy_seq" if await self._table_has_column("jobs", "legacy_seq") else "NULL AS legacy_seq"
        cursor = await conn.execute(
            f"""
            SELECT id, kind, user_id, state, source_kind, revision, accepted_at, updated_at,
                   resume_state, source_chat_id, source_url, status_chat_id, status_message_id,
                   {legacy_expr}, spoiler
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
        legacy_expr = "legacy_seq" if await self._table_has_column("jobs", "legacy_seq") else "NULL AS legacy_seq"
        sql = (
            "SELECT id, kind, user_id, state, source_kind, revision, accepted_at, updated_at, resume_state, "
            f"source_chat_id, source_url, status_chat_id, status_message_id, {legacy_expr}, spoiler FROM jobs"
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

    async def count_jobs_by_state(self, *, user_id: int | None = None) -> dict[str, int]:
        conn = self._require_conn()
        if user_id is None:
            cursor = await conn.execute("SELECT state,COUNT(*) AS n FROM jobs GROUP BY state")
        else:
            cursor = await conn.execute(
                "SELECT state,COUNT(*) AS n FROM jobs WHERE user_id=? GROUP BY state",
                (int(user_id),),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row["state"]): int(row["n"]) for row in rows}

    async def cleanup_inventory(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """Return durable facts needed to build a safe disk-cleanup dry run.

        This deliberately returns data only.  Filesystem validation and retention
        policy live in ``DiskManager`` so the repository never deletes paths.
        """
        conn = self._require_conn()
        limit = max(1, min(int(limit), 5000))
        cursor = await conn.execute(
            """
            SELECT
                j.id AS job_id,
                j.legacy_seq,
                j.state,
                j.local_dir,
                j.updated_at,
                j.finished_at,
                j.next_retry_at,
                j.claim_owner,
                j.claim_kind,
                COALESCE((
                    SELECT SUM(i.size_bytes)
                    FROM job_items i
                    WHERE i.job_id=j.id AND i.local_path IS NOT NULL
                ),0) AS item_bytes,
                (
                    SELECT a.state
                    FROM backup_attempts a
                    WHERE a.job_id=j.id
                    ORDER BY a.id DESC LIMIT 1
                ) AS backup_state,
                (
                    SELECT a.next_retry_at
                    FROM backup_attempts a
                    WHERE a.job_id=j.id
                    ORDER BY a.id DESC LIMIT 1
                ) AS backup_next_retry_at
            FROM jobs j
            WHERE j.local_dir IS NOT NULL
            ORDER BY COALESCE(j.finished_at,j.updated_at) ASC, j.id ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def page_jobs(
        self,
        *,
        user_id: int,
        filter_name: str = "all",
        page: int = 0,
        page_size: int = 5,
        completed_since: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one SQL-backed queue page and total count.

        The filter is an allowlisted UI value; no caller-provided SQL fragments
        are interpolated. Terminal rows are optionally bounded by updated_at so
        the default queue view does not grow forever.
        """
        conn = self._require_conn()
        page = max(0, int(page))
        page_size = max(1, min(int(page_size), 20))
        filters = {
            "all": None,
            "running": ("downloading", "publishing"),
            "waiting": ("collecting", "awaiting_confirmation", "queued", "ready", "interrupted"),
            "paused": ("paused",),
            "failed": ("failed",),
            "completed": ("succeeded", "cancelled"),
        }
        if filter_name not in filters:
            raise RepositoryError(f"unsupported queue filter: {filter_name}")
        clauses = ["user_id=?"]
        params: list[Any] = [int(user_id)]
        states = filters[filter_name]
        if states:
            marks = ",".join("?" for _ in states)
            clauses.append(f"state IN ({marks})")
            params.extend(states)
        if completed_since is not None:
            if filter_name == "completed":
                clauses.append("updated_at>=?")
                params.append(float(completed_since))
            elif filter_name == "all":
                clauses.append("(state NOT IN ('succeeded','cancelled') OR updated_at>=?)")
                params.append(float(completed_since))
        where = " AND ".join(clauses)
        cursor = await conn.execute(f"SELECT COUNT(*) AS n FROM jobs WHERE {where}", tuple(params))
        row = await cursor.fetchone()
        await cursor.close()
        total = int(row["n"] if row is not None else 0)
        cursor = await conn.execute(
            f"""
            SELECT id,legacy_seq,kind,state,revision,source_kind,bytes_done,bytes_total,
                   current_item,total_items,retry_count,error_code,error_message,updated_at
            FROM jobs
            WHERE {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, page * page_size),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows], total

    async def job_detail(self, job_id: int, *, user_id: int) -> dict[str, Any] | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT id,legacy_seq,kind,user_id,state,resume_state,download_state,publish_state,
                   backup_state,source_kind,bytes_done,bytes_total,current_item,total_items,
                   retry_count,next_retry_at,error_code,error_message,revision,accepted_at,started_at,
                   updated_at,finished_at
            FROM jobs WHERE id=? AND user_id=?
            """,
            (int(job_id), int(user_id)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        detail = dict(row)
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n,COALESCE(SUM(size_bytes),0) AS bytes FROM job_items WHERE job_id=?",
            (int(job_id),),
        )
        item_row = await cursor.fetchone()
        await cursor.close()
        detail["item_count"] = int(item_row["n"] if item_row else 0)
        detail["item_bytes"] = int(item_row["bytes"] if item_row else 0)
        cursor = await conn.execute(
            "SELECT local_path,size_bytes FROM job_items WHERE job_id=? ORDER BY ordinal",
            (int(job_id),),
        )
        detail["items"] = [dict(item) for item in await cursor.fetchall()]
        await cursor.close()
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM published_messages WHERE job_id=? AND deleted_at IS NULL",
            (int(job_id),),
        )
        pub = await cursor.fetchone()
        await cursor.close()
        detail["published_count"] = int(pub["n"] if pub else 0)
        cursor = await conn.execute(
            """
            SELECT state,remote_dir,retry_count,error_code,error_message,updated_at
            FROM backup_attempts WHERE job_id=? ORDER BY id DESC LIMIT 1
            """,
            (int(job_id),),
        )
        backup = await cursor.fetchone()
        await cursor.close()
        detail["backup"] = dict(backup) if backup is not None else None
        return detail

    async def batch_targets(self, *, user_id: int, kind: str) -> list[dict[str, Any]]:
        conn = self._require_conn()
        if kind == "waiting":
            states = ("collecting", "awaiting_confirmation", "queued", "ready", "interrupted")
        elif kind == "failed":
            states = ("failed",)
        else:
            raise RepositoryError(f"unsupported batch target kind: {kind}")
        marks = ",".join("?" for _ in states)
        cursor = await conn.execute(
            f"""
            SELECT j.id,j.revision,j.legacy_seq,j.state,
                   COALESCE((SELECT SUM(size_bytes) FROM job_items i WHERE i.job_id=j.id AND i.local_path IS NOT NULL),0) AS cache_bytes
            FROM jobs j
            WHERE j.user_id=? AND j.state IN ({marks})
            ORDER BY j.id
            """,
            (int(user_id), *states),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def set_status_reference(
        self,
        job_id: int,
        *,
        chat_id: int | None,
        message_id: int | None,
    ) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                "UPDATE jobs SET status_chat_id=?,status_message_id=?,updated_at=? WHERE id=?",
                (chat_id, message_id, time.time(), int(job_id)),
            )
            await conn.commit()

    async def update_job_progress(
        self,
        job_id: int,
        *,
        bytes_done: int,
        bytes_total: int,
        current_item: int,
        total_items: int,
    ) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                """
                UPDATE jobs
                SET bytes_done=?,bytes_total=?,current_item=?,total_items=?,updated_at=?
                WHERE id=? AND state IN ('downloading','publishing')
                """,
                (
                    max(0, int(bytes_done)),
                    max(0, int(bytes_total)),
                    max(1, int(current_item)),
                    max(1, int(total_items)),
                    time.time(),
                    int(job_id),
                ),
            )
            await conn.commit()

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
            resume_state=row["resume_state"],
            legacy_seq=row["legacy_seq"],
            spoiler=bool(row["spoiler"]),
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

    async def ensure_backup_attempt(
        self,
        *,
        job_id: int,
        remote_dir: str,
        now: float | None = None,
    ) -> BackupAttemptRecord:
        """Return the newest matching attempt or create one for legacy JSON logs."""
        attempts = await self.list_backup_attempts(int(job_id))
        for attempt in reversed(attempts):
            if attempt.remote_dir == str(remote_dir) and attempt.state != "succeeded":
                return attempt
        return await self.create_backup_attempt(
            job_id=int(job_id), state="running", remote_dir=str(remote_dir), now=now
        )

    async def ensure_backup_file(
        self,
        *,
        attempt_id: int,
        local_path: str,
        remote_name: str,
        size_bytes: int,
    ) -> BackupFileStatusRecord:
        local_path = self._validated_local_path(local_path)
        conn = self._require_conn()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                await conn.execute(
                    """INSERT OR IGNORE INTO backup_files(
                           attempt_id,local_path,remote_name,size_bytes,state
                       ) VALUES (?,?,?,?, 'pending')""",
                    (int(attempt_id), local_path, str(remote_name), int(size_bytes)),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        cursor = await conn.execute(
            """SELECT id,attempt_id,local_path,remote_name,size_bytes,state,bytes_done,
                      error_code,error_message
               FROM backup_files WHERE attempt_id=? AND remote_name=?""",
            (int(attempt_id), str(remote_name)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RepositoryError("backup file could not be ensured")
        return BackupFileStatusRecord(
            id=int(row["id"]), attempt_id=int(row["attempt_id"]),
            local_path=str(row["local_path"]), remote_name=str(row["remote_name"]),
            size_bytes=int(row["size_bytes"]), state=str(row["state"]),
            bytes_done=int(row["bytes_done"]), error_code=row["error_code"],
            error_message=row["error_message"],
        )

    async def update_backup_file_status(
        self,
        file_id: int,
        *,
        state: str,
        bytes_done: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        conn = self._require_conn()
        safe_message = _sanitize_error_message(error_message) if error_message else None
        async with self._write_lock:
            await conn.execute(
                """UPDATE backup_files
                   SET state=?,bytes_done=COALESCE(?,bytes_done),error_code=?,error_message=?
                   WHERE id=?""",
                (str(state), bytes_done, error_code, safe_message, int(file_id)),
            )
            await conn.commit()

    async def update_backup_attempt_status(
        self,
        attempt_id: int,
        *,
        state: str,
        retry_count: int | None = None,
        next_retry_at: float | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        finished: bool = False,
        now: float | None = None,
    ) -> None:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        safe_message = _sanitize_error_message(error_message) if error_message else None
        async with self._write_lock:
            await conn.execute(
                """UPDATE backup_attempts
                   SET state=?,retry_count=COALESCE(?,retry_count),next_retry_at=?,
                       error_code=?,error_message=?,updated_at=?,
                       finished_at=CASE WHEN ? THEN ? ELSE finished_at END
                   WHERE id=?""",
                (
                    str(state), retry_count, next_retry_at, error_code, safe_message,
                    timestamp, 1 if finished else 0, timestamp, int(attempt_id),
                ),
            )
            await conn.commit()

    async def backup_retry_due(
        self,
        *,
        legacy_seq: int,
        remote_dir: str,
        now: float | None = None,
    ) -> tuple[bool, float | None]:
        """Return whether the newest durable backup attempt is due for retry."""
        conn = self._require_conn()
        cursor = await conn.execute(
            """SELECT ba.next_retry_at
               FROM backup_attempts ba
               JOIN jobs j ON j.id=ba.job_id
               WHERE j.legacy_seq=? AND ba.remote_dir=?
               ORDER BY ba.id DESC LIMIT 1""",
            (int(legacy_seq), str(remote_dir)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row["next_retry_at"] is None:
            return True, None
        retry_at = float(row["next_retry_at"])
        current = time.time() if now is None else float(now)
        return current >= retry_at, retry_at

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
