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
    destination_profile_id: int | None = None
    destination_profile_snapshot_json: str | None = None


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
    content_sha256: str | None = None


@dataclass(frozen=True)
class DedupEntryRecord:
    id: int
    sha256: str
    size_bytes: int
    media_kind: str
    destination_key: str
    source_peer_id: int
    source_message_id: int
    media_id: int | None
    access_hash: int | None
    file_reference: bytes | None
    metadata_json: str | None
    verified_at: float
    last_used_at: float
    hit_count: int


@dataclass(frozen=True)
class DestinationProfileRecord:
    id: int
    name: str
    destination_peer: str
    discussion_group_peer: str | None
    channel_at: str
    group_at: str
    cover_mode: bool
    forward_caption: bool
    default_spoiler_mode: str
    backup_policy: str
    footer_template: str
    enabled: bool
    is_default: bool
    read_only: bool
    source_kind: str
    verified_at: float | None
    created_at: float
    updated_at: float


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
            "daily_stats",
            "stat_metric_applied",
            "dedup_entries",
            "destination_profiles",
        }
    )

    _STAT_COLUMNS = frozenset(
        {
            "accepted_jobs",
            "succeeded_jobs",
            "failed_jobs",
            "cancelled_jobs",
            "downloaded_bytes",
            "published_bytes",
            "backed_up_bytes",
            "saved_upload_bytes",
        }
    )

    @staticmethod
    def _day_utc(timestamp: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(float(timestamp)))

    async def _apply_stat_metric_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        scope_key: str,
        metric: str,
        value: int,
        timestamp: float,
    ) -> bool:
        if metric not in self._STAT_COLUMNS:
            raise RepositoryError(f"unsupported stat metric: {metric}")
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stat_metric_applied'"
        )
        available = await cursor.fetchone()
        await cursor.close()
        if available is None:
            return False
        value = max(0, int(value))
        day_utc = self._day_utc(timestamp)
        cursor = await conn.execute(
            """INSERT OR IGNORE INTO stat_metric_applied(scope_key,metric,day_utc,value,created_at)
               VALUES (?,?,?,?,?)""",
            (str(scope_key), str(metric), day_utc, value, float(timestamp)),
        )
        inserted = cursor.rowcount == 1
        await cursor.close()
        if not inserted:
            return False
        await conn.execute(
            "INSERT OR IGNORE INTO daily_stats(day_utc,updated_at) VALUES (?,?)",
            (day_utc, float(timestamp)),
        )
        await conn.execute(
            f"UPDATE daily_stats SET {metric}={metric}+?,updated_at=? WHERE day_utc=?",
            (value, float(timestamp), day_utc),
        )
        return True

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
                await self._apply_stat_metric_tx(
                    conn,
                    scope_key=f"job:{job_id}",
                    metric="accepted_jobs",
                    value=1,
                    timestamp=timestamp,
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
        destination_profile_id: int | None = None,
        destination_profile_snapshot: dict[str, Any] | None = None,
        event_type: str = "accepted",
        event_payload: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> JobRecord:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        payload_json = self._encode_versioned_payload(event_payload)
        profile_snapshot_json = self._encode_versioned_payload(destination_profile_snapshot)
        prepared = []
        for ordinal, item in enumerate(items or [], start=1):
            metadata_json = self._encode_versioned_payload(item.get("metadata"))
            local_path = item.get("local_path")
            if local_path:
                local_path = self._validated_local_path(str(local_path))
            prepared.append((ordinal, item, local_path, metadata_json))
        has_legacy_seq = await self._table_has_column("jobs", "legacy_seq")
        has_destination_profile = await self._table_has_column("jobs", "destination_profile_id")
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                if has_legacy_seq and has_destination_profile:
                    cursor = await conn.execute(
                        """
                        INSERT INTO jobs(
                          kind, user_id, state, spoiler, source_kind, source_chat_id,
                          source_url, legacy_seq, total_items, destination_profile_id,
                          destination_profile_snapshot_json, accepted_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (kind, user_id, state, int(spoiler), source_kind, source_chat_id,
                         source_url, legacy_seq, len(prepared), destination_profile_id,
                         profile_snapshot_json, timestamp, timestamp),
                    )
                elif has_legacy_seq:
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
                elif has_destination_profile:
                    cursor = await conn.execute(
                        """
                        INSERT INTO jobs(
                          kind, user_id, state, spoiler, source_kind, source_chat_id,
                          source_url, total_items, destination_profile_id,
                          destination_profile_snapshot_json, accepted_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (kind, user_id, state, int(spoiler), source_kind, source_chat_id,
                         source_url, len(prepared), destination_profile_id,
                        profile_snapshot_json, timestamp, timestamp),
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
                await self._apply_stat_metric_tx(
                    conn,
                    scope_key=f"job:{job_id}",
                    metric="accepted_jobs",
                    value=1,
                    timestamp=timestamp,
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
                if plan.to_state == "failed":
                    await self._apply_stat_metric_tx(
                        conn,
                        scope_key=f"job:{job_id}",
                        metric="failed_jobs",
                        value=1,
                        timestamp=timestamp,
                    )
                elif plan.to_state == "cancelled":
                    await self._apply_stat_metric_tx(
                        conn,
                        scope_key=f"job:{job_id}",
                        metric="cancelled_jobs",
                        value=1,
                        timestamp=timestamp,
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
                cursor = await conn.execute(
                    "SELECT COALESCE(SUM(size_bytes),0) AS n FROM job_items WHERE job_id=?",
                    (int(job_id),),
                )
                size_row = await cursor.fetchone()
                await cursor.close()
                await self._apply_stat_metric_tx(
                    conn,
                    scope_key=f"job:{job_id}",
                    metric="downloaded_bytes",
                    value=int(size_row["n"] if size_row is not None else 0),
                    timestamp=timestamp,
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
        has_hash = await self._table_has_column("job_items", "content_sha256")
        hash_expr = "content_sha256" if has_hash else "NULL AS content_sha256"
        cursor = await conn.execute(
            f"SELECT id,job_id,ordinal,source_chat_id,source_message_id,grouped_id,media_kind,local_path,size_bytes,metadata_json,{hash_expr} FROM job_items WHERE job_id=? ORDER BY ordinal",
            (job_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [JobItemRecord(int(r["id"]), int(r["job_id"]), int(r["ordinal"]), r["source_chat_id"], r["source_message_id"], r["grouped_id"], r["media_kind"], r["local_path"], int(r["size_bytes"]), r["metadata_json"], r["content_sha256"]) for r in rows]

    async def set_job_item_content_hashes(
        self, legacy_seq: int, hashes: list[tuple[str, int]]
    ) -> int:
        """Persist D1 hashes by durable item ordinal after download ordering is fixed."""
        if not hashes or not await self._table_has_column("job_items", "content_sha256"):
            return 0
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("SELECT id FROM jobs WHERE legacy_seq=?", (int(legacy_seq),))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return 0
            job_id = int(row["id"])
            changed = 0
            try:
                await conn.execute("BEGIN IMMEDIATE")
                for ordinal, (digest, size_bytes) in enumerate(hashes, start=1):
                    cursor = await conn.execute(
                        """UPDATE job_items SET content_sha256=?,size_bytes=CASE WHEN size_bytes=0 THEN ? ELSE size_bytes END
                           WHERE job_id=? AND ordinal=?""",
                        (str(digest), max(0, int(size_bytes)), job_id, ordinal),
                    )
                    changed += max(0, int(cursor.rowcount or 0))
                    await cursor.close()
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return changed

    async def set_job_item_media_metadata(
        self,
        legacy_seq: int,
        values: list[dict[str, Any] | None],
    ) -> int:
        """Merge normalized M1 metadata into job_items.metadata_json by ordinal."""
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("SELECT id FROM jobs WHERE legacy_seq=?", (int(legacy_seq),))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return 0
            job_id = int(row["id"])
            cursor = await conn.execute(
                "SELECT id,metadata_json FROM job_items WHERE job_id=? ORDER BY ordinal",
                (job_id,),
            )
            items = await cursor.fetchall()
            await cursor.close()
            changed = 0
            try:
                await conn.execute("BEGIN IMMEDIATE")
                for index, item in enumerate(items):
                    if index >= len(values) or values[index] is None:
                        continue
                    payload: dict[str, Any] = {"schema_version": 1}
                    raw = item["metadata_json"]
                    if raw:
                        try:
                            decoded = json.loads(raw)
                            if isinstance(decoded, dict):
                                payload.update(decoded)
                        except Exception:
                            pass
                    payload["media_compat"] = dict(values[index] or {})
                    await conn.execute(
                        "UPDATE job_items SET metadata_json=? WHERE id=?",
                        (self._encode_versioned_payload(payload), int(item["id"])),
                    )
                    changed += 1
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return changed

    async def lookup_dedup_entry(
        self, *, sha256: str, size_bytes: int, media_kind: str, destination_key: str
    ) -> DedupEntryRecord | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            """SELECT id,sha256,size_bytes,media_kind,destination_key,source_peer_id,source_message_id,
                      media_id,access_hash,file_reference,metadata_json,verified_at,last_used_at,hit_count
               FROM dedup_entries
               WHERE sha256=? AND size_bytes=? AND media_kind=? AND destination_key=?""",
            (str(sha256), int(size_bytes), str(media_kind), str(destination_key)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._dedup_from_row(row) if row is not None else None

    async def upsert_dedup_entry(
        self,
        *,
        sha256: str,
        size_bytes: int,
        media_kind: str,
        destination_key: str,
        source_peer_id: int,
        source_message_id: int,
        media_id: int | None = None,
        access_hash: int | None = None,
        file_reference: bytes | None = None,
        metadata: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> DedupEntryRecord:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        metadata_json = self._encode_versioned_payload(metadata)
        async with self._write_lock:
            await conn.execute(
                """
                INSERT INTO dedup_entries(
                  sha256,size_bytes,media_kind,destination_key,source_peer_id,source_message_id,
                  media_id,access_hash,file_reference,metadata_json,verified_at,last_used_at,hit_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(sha256,size_bytes,media_kind,destination_key) DO UPDATE SET
                  source_peer_id=excluded.source_peer_id,
                  source_message_id=excluded.source_message_id,
                  media_id=excluded.media_id,
                  access_hash=excluded.access_hash,
                  file_reference=excluded.file_reference,
                  metadata_json=excluded.metadata_json,
                  verified_at=excluded.verified_at,
                  last_used_at=excluded.last_used_at
                """,
                (
                    str(sha256), int(size_bytes), str(media_kind), str(destination_key),
                    int(source_peer_id), int(source_message_id), media_id, access_hash,
                    file_reference, metadata_json, timestamp, timestamp,
                ),
            )
            await conn.commit()
        found = await self.lookup_dedup_entry(
            sha256=sha256, size_bytes=size_bytes, media_kind=media_kind, destination_key=destination_key
        )
        if found is None:
            raise RepositoryError("dedup entry missing after upsert")
        return found

    async def mark_dedup_hit(self, entry_id: int, *, saved_bytes: int, now: float | None = None) -> None:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                await conn.execute(
                    "UPDATE dedup_entries SET hit_count=hit_count+1,last_used_at=? WHERE id=?",
                    (timestamp, int(entry_id)),
                )
                await self._apply_stat_metric_tx(
                    conn,
                    scope_key=f"dedup_hit:{int(entry_id)}:{int(timestamp * 1000)}",
                    metric="saved_upload_bytes",
                    value=max(0, int(saved_bytes)),
                    timestamp=timestamp,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def delete_dedup_entry(self, entry_id: int) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute("DELETE FROM dedup_entries WHERE id=?", (int(entry_id),))
            await conn.commit()

    @staticmethod
    def _dedup_from_row(row: aiosqlite.Row) -> DedupEntryRecord:
        return DedupEntryRecord(
            id=int(row["id"]), sha256=str(row["sha256"]), size_bytes=int(row["size_bytes"]),
            media_kind=str(row["media_kind"]), destination_key=str(row["destination_key"]),
            source_peer_id=int(row["source_peer_id"]), source_message_id=int(row["source_message_id"]),
            media_id=row["media_id"], access_hash=row["access_hash"], file_reference=row["file_reference"],
            metadata_json=row["metadata_json"], verified_at=float(row["verified_at"]),
            last_used_at=float(row["last_used_at"]), hit_count=int(row["hit_count"]),
        )

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

    async def count_backup_attempts(self) -> int:
        conn = self._require_conn()
        cursor = await conn.execute("SELECT COUNT(*) FROM backup_attempts")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0] if row else 0)

    async def list_backup_attempt_page(self, *, limit: int = 5, offset: int = 0) -> list[dict[str, Any]]:
        """List durable backup attempts with aggregate file counts for Telegram UI."""
        conn = self._require_conn()
        limit = max(1, min(int(limit), 20))
        offset = max(0, int(offset))
        cursor = await conn.execute(
            """
            SELECT a.id,a.job_id,j.legacy_seq,a.state,a.remote_dir,a.retry_count,a.next_retry_at,
                   a.error_code,a.created_at,a.updated_at,a.finished_at,
                   COUNT(f.id) AS total_files,
                   COALESCE(SUM(f.size_bytes),0) AS total_bytes,
                   SUM(CASE WHEN f.state='succeeded' THEN 1 ELSE 0 END) AS succeeded_files,
                   SUM(CASE WHEN f.state='failed' THEN 1 ELSE 0 END) AS failed_files
            FROM backup_attempts a
            JOIN jobs j ON j.id=a.job_id
            LEFT JOIN backup_files f ON f.attempt_id=a.id
            GROUP BY a.id
            ORDER BY a.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def backup_attempt_detail(self, attempt_id: int) -> dict[str, Any] | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT a.id,a.job_id,j.legacy_seq,a.state,a.remote_dir,a.retry_count,a.next_retry_at,
                   a.error_code,a.error_message,a.created_at,a.updated_at,a.finished_at
            FROM backup_attempts a JOIN jobs j ON j.id=a.job_id
            WHERE a.id=?
            """,
            (int(attempt_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def count_backup_files(self, attempt_id: int) -> int:
        conn = self._require_conn()
        cursor = await conn.execute("SELECT COUNT(*) FROM backup_files WHERE attempt_id=?", (int(attempt_id),))
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0] if row else 0)

    async def list_backup_file_page(
        self, attempt_id: int, *, limit: int = 5, offset: int = 0
    ) -> list[dict[str, Any]]:
        conn = self._require_conn()
        limit = max(1, min(int(limit), 20))
        offset = max(0, int(offset))
        cursor = await conn.execute(
            """
            SELECT id,attempt_id,remote_name,size_bytes,state,bytes_done,error_code,error_message,
                   CASE WHEN local_path <> '' THEN 1 ELSE 0 END AS has_local_path
            FROM backup_files
            WHERE attempt_id=?
            ORDER BY id
            LIMIT ? OFFSET ?
            """,
            (int(attempt_id), limit, offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def backup_file_detail(self, file_id: int) -> dict[str, Any] | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT f.id,f.attempt_id,f.local_path,f.remote_name,f.size_bytes,f.state,f.bytes_done,
                   f.error_code,f.error_message,a.job_id,a.remote_dir,j.legacy_seq
            FROM backup_files f
            JOIN backup_attempts a ON a.id=f.attempt_id
            JOIN jobs j ON j.id=a.job_id
            WHERE f.id=?
            """,
            (int(file_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def backup_retry_file_ids(self, attempt_id: int) -> list[int]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id FROM backup_files WHERE attempt_id=? AND state NOT IN ('succeeded','deleted') ORDER BY id",
            (int(attempt_id),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [int(row[0]) for row in rows]

    async def backup_attempt_file_summary(self, attempt_id: int) -> dict[str, Any]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN state NOT IN ('succeeded','deleted') THEN 1 ELSE 0 END) AS failed,
                   MIN(CASE WHEN state NOT IN ('succeeded','deleted') THEN error_code END) AS error_code,
                   MIN(CASE WHEN state NOT IN ('succeeded','deleted') THEN error_message END) AS error_message
            FROM backup_files WHERE attempt_id=?
            """,
            (int(attempt_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else {"total": 0, "failed": 0}

    async def latest_backup_attempt_for_legacy_seq(self, legacy_seq: int) -> dict[str, Any] | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT a.id,a.job_id,a.state,a.remote_dir,a.retry_count,a.next_retry_at,
                   a.error_code,a.error_message,a.created_at,a.updated_at,a.finished_at
            FROM backup_attempts a
            JOIN jobs j ON j.id=a.job_id
            WHERE j.legacy_seq=?
            ORDER BY a.id DESC LIMIT 1
            """,
            (int(legacy_seq),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def due_backup_retry_file_ids(self, *, now: float | None = None, limit: int = 20) -> list[int]:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        limit = max(1, min(int(limit), 100))
        cursor = await conn.execute(
            """
            SELECT f.id
            FROM backup_files f
            JOIN backup_attempts a ON a.id=f.attempt_id
            WHERE f.state NOT IN ('succeeded','deleted')
              AND COALESCE(f.error_code,'') <> 'cache_missing'
              AND a.state IN ('failed','retry_wait','interrupted')
              AND (a.next_retry_at IS NULL OR a.next_retry_at<=?)
            ORDER BY a.id,f.id
            LIMIT ?
            """,
            (timestamp, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [int(row[0]) for row in rows]

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
                cursor = await conn.execute(
                    "SELECT COALESCE(SUM(size_bytes),0) AS n FROM job_items WHERE job_id=?",
                    (int(job_id),),
                )
                size_row = await cursor.fetchone()
                await cursor.close()
                published_bytes = int(size_row["n"] if size_row is not None else 0)
                await self._apply_stat_metric_tx(
                    conn,
                    scope_key=f"job:{job_id}",
                    metric="succeeded_jobs",
                    value=1,
                    timestamp=timestamp,
                )
                await self._apply_stat_metric_tx(
                    conn,
                    scope_key=f"job:{job_id}",
                    metric="published_bytes",
                    value=published_bytes,
                    timestamp=timestamp,
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
        profile_id_expr = (
            "destination_profile_id"
            if await self._table_has_column("jobs", "destination_profile_id")
            else "NULL AS destination_profile_id"
        )
        profile_snapshot_expr = (
            "destination_profile_snapshot_json"
            if await self._table_has_column("jobs", "destination_profile_snapshot_json")
            else "NULL AS destination_profile_snapshot_json"
        )
        cursor = await conn.execute(
            f"""
            SELECT id, kind, user_id, state, source_kind, revision, accepted_at, updated_at,
                   resume_state, source_chat_id, source_url, status_chat_id, status_message_id,
                   {legacy_expr}, spoiler, {profile_id_expr}, {profile_snapshot_expr}
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
        profile_id_expr = (
            "destination_profile_id"
            if await self._table_has_column("jobs", "destination_profile_id")
            else "NULL AS destination_profile_id"
        )
        profile_snapshot_expr = (
            "destination_profile_snapshot_json"
            if await self._table_has_column("jobs", "destination_profile_snapshot_json")
            else "NULL AS destination_profile_snapshot_json"
        )
        sql = (
            "SELECT id, kind, user_id, state, source_kind, revision, accepted_at, updated_at, resume_state, "
            f"source_chat_id, source_url, status_chat_id, status_message_id, {legacy_expr}, spoiler, "
            f"{profile_id_expr}, {profile_snapshot_expr} FROM jobs"
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

    async def reconcile_daily_stats(self) -> int:
        """Idempotently backfill materialized stats from durable source rows."""
        conn = self._require_conn()
        applied = 0
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """SELECT id,state,download_state,accepted_at,updated_at,finished_at
                       FROM jobs ORDER BY id"""
                )
                jobs = await cursor.fetchall()
                await cursor.close()
                for row in jobs:
                    job_id = int(row["id"])
                    accepted_at = float(row["accepted_at"])
                    applied += int(await self._apply_stat_metric_tx(
                        conn, scope_key=f"job:{job_id}", metric="accepted_jobs",
                        value=1, timestamp=accepted_at,
                    ))
                    if str(row["download_state"]) == "succeeded":
                        size_cursor = await conn.execute(
                            "SELECT COALESCE(SUM(size_bytes),0) AS n FROM job_items WHERE job_id=?",
                            (job_id,),
                        )
                        size_row = await size_cursor.fetchone()
                        await size_cursor.close()
                        applied += int(await self._apply_stat_metric_tx(
                            conn, scope_key=f"job:{job_id}", metric="downloaded_bytes",
                            value=int(size_row["n"] if size_row else 0),
                            timestamp=float(row["updated_at"]),
                        ))
                    terminal_at = float(row["finished_at"] or row["updated_at"])
                    state = str(row["state"])
                    if state == "succeeded":
                        size_cursor = await conn.execute(
                            "SELECT COALESCE(SUM(size_bytes),0) AS n FROM job_items WHERE job_id=?",
                            (job_id,),
                        )
                        size_row = await size_cursor.fetchone()
                        await size_cursor.close()
                        applied += int(await self._apply_stat_metric_tx(
                            conn, scope_key=f"job:{job_id}", metric="succeeded_jobs",
                            value=1, timestamp=terminal_at,
                        ))
                        applied += int(await self._apply_stat_metric_tx(
                            conn, scope_key=f"job:{job_id}", metric="published_bytes",
                            value=int(size_row["n"] if size_row else 0), timestamp=terminal_at,
                        ))
                    elif state == "failed":
                        applied += int(await self._apply_stat_metric_tx(
                            conn, scope_key=f"job:{job_id}", metric="failed_jobs",
                            value=1, timestamp=terminal_at,
                        ))
                    elif state == "cancelled":
                        applied += int(await self._apply_stat_metric_tx(
                            conn, scope_key=f"job:{job_id}", metric="cancelled_jobs",
                            value=1, timestamp=terminal_at,
                        ))
                cursor = await conn.execute(
                    """SELECT f.id,f.size_bytes,a.updated_at
                       FROM backup_files f
                       JOIN backup_attempts a ON a.id=f.attempt_id
                       WHERE f.state='succeeded' ORDER BY f.id"""
                )
                files = await cursor.fetchall()
                await cursor.close()
                for row in files:
                    applied += int(await self._apply_stat_metric_tx(
                        conn,
                        scope_key=f"backup_file:{int(row['id'])}",
                        metric="backed_up_bytes",
                        value=int(row["size_bytes"]),
                        timestamp=float(row["updated_at"]),
                    ))
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return applied

    async def stats_snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        today = self._day_utc(timestamp)
        columns = sorted(self._STAT_COLUMNS)
        projection = ",".join(columns)
        cursor = await conn.execute(
            f"SELECT {projection} FROM daily_stats WHERE day_utc=?",
            (today,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        today_values = {column: int(row[column]) if row is not None else 0 for column in columns}
        cursor = await conn.execute(
            "SELECT " + ",".join(f"COALESCE(SUM({column}),0) AS {column}" for column in columns) + " FROM daily_stats"
        )
        total_row = await cursor.fetchone()
        await cursor.close()
        totals = {column: int(total_row[column]) if total_row is not None else 0 for column in columns}
        cursor = await conn.execute(
            """SELECT error_code,COUNT(*) AS n FROM jobs
               WHERE error_code IS NOT NULL AND error_code<>''
               GROUP BY error_code ORDER BY MAX(updated_at) DESC LIMIT 5"""
        )
        errors = [{"code": str(item["error_code"]), "count": int(item["n"])} for item in await cursor.fetchall()]
        await cursor.close()
        return {"day_utc": today, "today": today_values, "totals": totals, "recent_errors": errors}

    async def stats_event_summary(self, *, limit: int = 8) -> list[dict[str, Any]]:
        """Return event counts/types only; never expose event payloads or user content."""
        conn = self._require_conn()
        limit = max(1, min(int(limit), 20))
        cursor = await conn.execute(
            """SELECT event_type,COUNT(*) AS n,MAX(created_at) AS last_at
               FROM job_events
               GROUP BY event_type
               ORDER BY last_at DESC,event_type
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "event_type": str(row["event_type"]),
                "count": int(row["n"]),
                "last_at": float(row["last_at"]),
            }
            for row in rows
        ]

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
                j.revision,
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

    async def acquire_disk_cleanup_claim(
        self,
        job_id: int,
        *,
        expected_revision: int,
        owner: str,
    ) -> JobRecord | None:
        """Reserve one terminal job for filesystem cleanup using revision CAS."""
        conn = self._require_conn()
        now = time.time()
        async with self._write_lock:
            cursor = await conn.execute(
                """
                UPDATE jobs
                SET claim_owner=?,claim_kind='cleanup',heartbeat_at=?,revision=revision+1,updated_at=?
                WHERE id=? AND revision=? AND state IN ('succeeded','failed','cancelled')
                  AND claim_owner IS NULL
                """,
                (str(owner), now, now, int(job_id), int(expected_revision)),
            )
            changed = int(cursor.rowcount or 0)
            await cursor.close()
            await conn.commit()
        if not changed:
            return None
        return await self.get_job(int(job_id))

    async def finish_disk_cleanup(
        self,
        job_id: int,
        *,
        expected_revision: int,
        owner: str,
        freed_bytes: int,
    ) -> bool:
        """Commit successful cleanup and clear durable local cache references."""
        conn = self._require_conn()
        now = time.time()
        payload = json.dumps(
            {"schema_version": 1, "freed_bytes": max(0, int(freed_bytes))},
            separators=(",", ":"),
            sort_keys=True,
        )
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """
                    SELECT state,revision,claim_owner,claim_kind
                    FROM jobs WHERE id=?
                    """,
                    (int(job_id),),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if (
                    row is None
                    or int(row["revision"]) != int(expected_revision)
                    or str(row["claim_owner"] or "") != str(owner)
                    or str(row["claim_kind"] or "") != "cleanup"
                    or str(row["state"]) not in {"succeeded", "failed", "cancelled"}
                ):
                    await conn.rollback()
                    return False
                state = str(row["state"])
                await conn.execute(
                    "UPDATE job_items SET local_path=NULL WHERE job_id=?",
                    (int(job_id),),
                )
                await conn.execute(
                    """
                    UPDATE jobs
                    SET local_dir=NULL,claim_owner=NULL,claim_kind=NULL,heartbeat_at=NULL,
                        revision=revision+1,updated_at=?
                    WHERE id=? AND revision=?
                    """,
                    (now, int(job_id), int(expected_revision)),
                )
                await conn.execute(
                    """
                    INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at)
                    VALUES (?,'disk_cleanup',?,?,?,?)
                    """,
                    (int(job_id), state, state, payload, now),
                )
                await conn.commit()
                return True
            except Exception:
                await conn.rollback()
                raise

    async def abort_disk_cleanup_claim(
        self,
        job_id: int,
        *,
        expected_revision: int,
        owner: str,
        freed_bytes: int = 0,
    ) -> bool:
        """Release a cleanup claim after partial filesystem failure."""
        conn = self._require_conn()
        now = time.time()
        payload = json.dumps(
            {"schema_version": 1, "freed_bytes": max(0, int(freed_bytes))},
            separators=(",", ":"),
            sort_keys=True,
        )
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """
                    SELECT state FROM jobs
                    WHERE id=? AND revision=? AND claim_owner=? AND claim_kind='cleanup'
                    """,
                    (int(job_id), int(expected_revision), str(owner)),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    await conn.rollback()
                    return False
                state = str(row["state"])
                cursor = await conn.execute(
                    """
                    UPDATE jobs
                    SET claim_owner=NULL,claim_kind=NULL,heartbeat_at=NULL,
                        revision=revision+1,updated_at=?
                    WHERE id=? AND revision=? AND claim_owner=? AND claim_kind='cleanup'
                    """,
                    (now, int(job_id), int(expected_revision), str(owner)),
                )
                changed = int(cursor.rowcount or 0)
                await cursor.close()
                if not changed:
                    await conn.rollback()
                    return False
                await conn.execute(
                    """
                    INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at)
                    VALUES (?,'disk_cleanup_failed',?,?,?,?)
                    """,
                    (int(job_id), state, state, payload, now),
                )
                await conn.commit()
                return True
            except Exception:
                await conn.rollback()
                raise

    async def release_interrupted_disk_cleanup_claims(self) -> int:
        """Release cleanup claims left by a previous process after restart."""
        conn = self._require_conn()
        now = time.time()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """
                    SELECT id,state FROM jobs
                    WHERE claim_kind='cleanup' AND claim_owner IS NOT NULL
                    ORDER BY id
                    """
                )
                rows = await cursor.fetchall()
                await cursor.close()
                for row in rows:
                    job_id = int(row["id"])
                    state = str(row["state"])
                    await conn.execute(
                        """
                        UPDATE jobs
                        SET claim_owner=NULL,claim_kind=NULL,heartbeat_at=NULL,
                            revision=revision+1,updated_at=?
                        WHERE id=? AND claim_kind='cleanup'
                        """,
                        (now, job_id),
                    )
                    await conn.execute(
                        """
                        INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at)
                        VALUES (?,'disk_cleanup_interrupted',?,?,?,?)
                        """,
                        (
                            job_id,
                            state,
                            state,
                            '{"schema_version":1}',
                            now,
                        ),
                    )
                await conn.commit()
                return len(rows)
            except Exception:
                await conn.rollback()
                raise

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
                   updated_at,finished_at,destination_profile_id,destination_profile_snapshot_json
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
            "SELECT local_path,size_bytes,metadata_json FROM job_items WHERE job_id=? ORDER BY ordinal",
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

    async def set_job_destination_profile(
        self,
        job_id: int,
        *,
        expected_revision: int,
        profile_id: int,
        snapshot: dict[str, Any],
    ) -> bool:
        """CAS-update an awaiting/queued job's immutable destination snapshot."""
        conn = self._require_conn()
        encoded = self._encode_versioned_payload(snapshot)
        timestamp = time.time()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """UPDATE jobs
                       SET destination_profile_id=?,destination_profile_snapshot_json=?,
                           revision=revision+1,updated_at=?
                       WHERE id=? AND revision=? AND state IN ('awaiting_confirmation','queued')""",
                    (
                        int(profile_id), encoded, timestamp,
                        int(job_id), int(expected_revision),
                    ),
                )
                changed = cursor.rowcount == 1
                await cursor.close()
                if not changed:
                    await conn.rollback()
                    return False
                await conn.execute(
                    """INSERT INTO job_events(job_id,event_type,from_state,to_state,payload_json,created_at)
                       SELECT id,'destination_profile_selected',state,state,?,? FROM jobs WHERE id=?""",
                    (
                        self._encode_versioned_payload(
                            {"schema_version": 1, "destination_profile_id": int(profile_id)}
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

    @staticmethod
    def _destination_profile_from_row(row: aiosqlite.Row) -> DestinationProfileRecord:
        return DestinationProfileRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            destination_peer=str(row["destination_peer"]),
            discussion_group_peer=(
                str(row["discussion_group_peer"])
                if row["discussion_group_peer"] is not None
                else None
            ),
            channel_at=str(row["channel_at"] or ""),
            group_at=str(row["group_at"] or ""),
            cover_mode=bool(row["cover_mode"]),
            forward_caption=bool(row["forward_caption"]),
            default_spoiler_mode=str(row["default_spoiler_mode"] or "ask"),
            backup_policy=str(row["backup_policy"] or "best_effort"),
            footer_template=str(row["footer_template"] or ""),
            enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
            read_only=bool(row["read_only"]),
            source_kind=str(row["source_kind"] or "user"),
            verified_at=(float(row["verified_at"]) if row["verified_at"] is not None else None),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def destination_profile_snapshot(profile: DestinationProfileRecord) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "profile_id": int(profile.id),
            "name": profile.name,
            "destination_peer": profile.destination_peer,
            "discussion_group_peer": profile.discussion_group_peer,
            "channel_at": profile.channel_at,
            "group_at": profile.group_at,
            "cover_mode": bool(profile.cover_mode),
            "forward_caption": bool(profile.forward_caption),
            "default_spoiler_mode": profile.default_spoiler_mode,
            "backup_policy": profile.backup_policy,
            "footer_template": profile.footer_template,
        }

    async def ensure_env_destination_profile(
        self,
        *,
        destination_peer: str,
        channel_at: str = "",
        group_at: str = "",
        cover_mode: bool = False,
        forward_caption: bool = False,
        default_spoiler_mode: str = "ask",
        backup_policy: str = "best_effort",
        now: float | None = None,
    ) -> DestinationProfileRecord:
        """Register the legacy env destination as the immutable compatibility profile."""
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        peer = str(destination_peer).strip()
        if not peer:
            raise RepositoryError("destination profile requires destination peer")
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT id FROM destination_profiles WHERE source_kind='env' ORDER BY id LIMIT 1"
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    cursor = await conn.execute(
                        "SELECT COUNT(*) AS n FROM destination_profiles WHERE is_default=1"
                    )
                    default_row = await cursor.fetchone()
                    await cursor.close()
                    is_default = 1 if int(default_row["n"] if default_row else 0) == 0 else 0
                    cursor = await conn.execute(
                        """INSERT INTO destination_profiles(
                               name,destination_peer,discussion_group_peer,channel_at,group_at,
                               cover_mode,forward_caption,default_spoiler_mode,backup_policy,
                               footer_template,enabled,is_default,read_only,source_kind,verified_at,created_at,updated_at
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            "默认频道", peer, None, str(channel_at or ""), str(group_at or ""),
                            int(bool(cover_mode)), int(bool(forward_caption)), str(default_spoiler_mode or "ask"),
                            str(backup_policy or "best_effort"), "", 1, is_default, 1, "env", timestamp, timestamp, timestamp,
                        ),
                    )
                    profile_id = int(cursor.lastrowid)
                    await cursor.close()
                else:
                    profile_id = int(row["id"])
                    await conn.execute(
                        """UPDATE destination_profiles
                           SET destination_peer=?,channel_at=?,group_at=?,cover_mode=?,forward_caption=?,
                               default_spoiler_mode=?,backup_policy=?,enabled=1,read_only=1,updated_at=?
                           WHERE id=?""",
                        (
                            peer, str(channel_at or ""), str(group_at or ""), int(bool(cover_mode)),
                            int(bool(forward_caption)), str(default_spoiler_mode or "ask"),
                            str(backup_policy or "best_effort"), timestamp, profile_id,
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        profile = await self.get_destination_profile(profile_id)
        if profile is None:
            raise RepositoryError("env destination profile could not be read back")
        return profile

    async def get_destination_profile(self, profile_id: int) -> DestinationProfileRecord | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT * FROM destination_profiles WHERE id=?", (int(profile_id),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._destination_profile_from_row(row) if row is not None else None

    async def get_default_destination_profile(self) -> DestinationProfileRecord | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT * FROM destination_profiles WHERE enabled=1 AND is_default=1 ORDER BY id LIMIT 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._destination_profile_from_row(row) if row is not None else None

    async def list_destination_profiles(self, *, enabled_only: bool = False) -> list[DestinationProfileRecord]:
        conn = self._require_conn()
        sql = "SELECT * FROM destination_profiles"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY is_default DESC, enabled DESC, id"
        cursor = await conn.execute(sql)
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._destination_profile_from_row(row) for row in rows]

    async def create_destination_profile(
        self,
        *,
        name: str,
        destination_peer: str,
        discussion_group_peer: str | None = None,
        channel_at: str = "",
        group_at: str = "",
        cover_mode: bool = False,
        forward_caption: bool = False,
        default_spoiler_mode: str = "ask",
        backup_policy: str = "best_effort",
        footer_template: str = "",
        now: float | None = None,
    ) -> DestinationProfileRecord:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        clean_name = str(name).strip()[:80]
        peer = str(destination_peer).strip()
        if not clean_name or not peer:
            raise RepositoryError("destination profile name and peer are required")
        if str(default_spoiler_mode) not in {"ask", "always_normal", "always_spoiler"}:
            raise RepositoryError("invalid default spoiler mode")
        if str(backup_policy) not in {"best_effort", "required"}:
            raise RepositoryError("invalid backup policy")
        footer = str(footer_template or "")
        if len(footer) > 512:
            raise RepositoryError("footer template is too long")
        async with self._write_lock:
            cursor = await conn.execute(
                """INSERT INTO destination_profiles(
                       name,destination_peer,discussion_group_peer,channel_at,group_at,
                       cover_mode,forward_caption,default_spoiler_mode,backup_policy,
                       footer_template,enabled,is_default,read_only,source_kind,verified_at,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    clean_name, peer,
                    str(discussion_group_peer).strip() if discussion_group_peer else None,
                    str(channel_at or ""), str(group_at or ""), int(bool(cover_mode)),
                    int(bool(forward_caption)), str(default_spoiler_mode), str(backup_policy), footer,
                    1, 0, 0, "user", None, timestamp, timestamp,
                ),
            )
            profile_id = int(cursor.lastrowid)
            await cursor.close()
            await conn.commit()
        profile = await self.get_destination_profile(profile_id)
        if profile is None:
            raise RepositoryError("created destination profile could not be read back")
        return profile

    async def set_default_destination_profile(self, profile_id: int) -> bool:
        conn = self._require_conn()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT enabled,verified_at FROM destination_profiles WHERE id=?", (int(profile_id),)
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None or not bool(row["enabled"]) or row["verified_at"] is None:
                    await conn.rollback()
                    return False
                await conn.execute("UPDATE destination_profiles SET is_default=0 WHERE is_default=1")
                await conn.execute(
                    "UPDATE destination_profiles SET is_default=1,updated_at=? WHERE id=?",
                    (time.time(), int(profile_id)),
                )
                await conn.commit()
                return True
            except Exception:
                await conn.rollback()
                raise

    async def update_destination_profile(self, profile_id: int, **changes: Any) -> str:
        allowed = {
            "name",
            "destination_peer",
            "discussion_group_peer",
            "channel_at",
            "group_at",
            "cover_mode",
            "forward_caption",
            "default_spoiler_mode",
            "backup_policy",
            "footer_template",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise RepositoryError("unsupported destination profile field")
        if not changes:
            return "noop"
        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            if key in {"cover_mode", "forward_caption"}:
                normalized[key] = int(bool(value))
            elif key == "discussion_group_peer":
                normalized[key] = str(value).strip() if value else None
            else:
                normalized[key] = str(value or "").strip()
        if "name" in normalized:
            normalized["name"] = normalized["name"][:80]
            if not normalized["name"]:
                raise RepositoryError("destination profile name is required")
        if "destination_peer" in normalized and not normalized["destination_peer"]:
            raise RepositoryError("destination peer is required")
        if "default_spoiler_mode" in normalized and normalized["default_spoiler_mode"] not in {
            "ask", "always_normal", "always_spoiler"
        }:
            raise RepositoryError("invalid default spoiler mode")
        if "backup_policy" in normalized and normalized["backup_policy"] not in {
            "best_effort", "required"
        }:
            raise RepositoryError("invalid backup policy")
        if "footer_template" in normalized and len(normalized["footer_template"]) > 512:
            raise RepositoryError("footer template is too long")
        conn = self._require_conn()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT read_only,is_default FROM destination_profiles WHERE id=?", (int(profile_id),)
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    await conn.rollback()
                    return "missing"
                if bool(row["read_only"]):
                    await conn.rollback()
                    return "read_only"
                routing_fields = {"destination_peer", "discussion_group_peer", "cover_mode"}
                if bool(row["is_default"]) and routing_fields & set(normalized):
                    await conn.rollback()
                    return "default_requires_switch"
                invalidate_verification = bool(
                    routing_fields & set(normalized)
                )
                assignments = ",".join(f"{key}=?" for key in normalized)
                if invalidate_verification:
                    assignments += ",verified_at=NULL"
                params = [normalized[key] for key in normalized]
                params.extend([time.time(), int(profile_id)])
                await conn.execute(
                    f"UPDATE destination_profiles SET {assignments},updated_at=? WHERE id=?",
                    tuple(params),
                )
                await conn.commit()
                return "ok"
            except Exception:
                await conn.rollback()
                raise

    async def mark_destination_profile_verified(
        self, profile_id: int, *, now: float | None = None
    ) -> bool:
        conn = self._require_conn()
        timestamp = time.time() if now is None else float(now)
        async with self._write_lock:
            cursor = await conn.execute(
                """UPDATE destination_profiles
                   SET verified_at=?,updated_at=?
                   WHERE id=? AND enabled=1""",
                (timestamp, timestamp, int(profile_id)),
            )
            changed = cursor.rowcount == 1
            await cursor.close()
            await conn.commit()
        return changed

    async def disable_destination_profile(self, profile_id: int) -> str:
        conn = self._require_conn()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    "SELECT enabled,is_default,read_only FROM destination_profiles WHERE id=?",
                    (int(profile_id),),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    await conn.rollback()
                    return "missing"
                if bool(row["read_only"]):
                    await conn.rollback()
                    return "read_only"
                if bool(row["is_default"]):
                    await conn.rollback()
                    return "default"
                cursor = await conn.execute(
                    """SELECT COUNT(*) AS n FROM jobs
                       WHERE destination_profile_id=? AND state NOT IN ('succeeded','failed','cancelled')""",
                    (int(profile_id),),
                )
                active = await cursor.fetchone()
                await cursor.close()
                if int(active["n"] if active else 0) > 0:
                    await conn.rollback()
                    return "in_use"
                await conn.execute(
                    "UPDATE destination_profiles SET enabled=0,updated_at=? WHERE id=?",
                    (time.time(), int(profile_id)),
                )
                await conn.commit()
                return "ok"
            except Exception:
                await conn.rollback()
                raise

    @staticmethod
    def _job_from_row(row: aiosqlite.Row) -> JobRecord:
        keys = set(row.keys())
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
            destination_profile_id=(
                row["destination_profile_id"] if "destination_profile_id" in keys else None
            ),
            destination_profile_snapshot_json=(
                row["destination_profile_snapshot_json"]
                if "destination_profile_snapshot_json" in keys
                else None
            ),
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
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """SELECT f.state,f.size_bytes,a.updated_at
                       FROM backup_files f JOIN backup_attempts a ON a.id=f.attempt_id
                       WHERE f.id=?""",
                    (int(file_id),),
                )
                previous = await cursor.fetchone()
                await cursor.close()
                await conn.execute(
                    """UPDATE backup_files
                       SET state=?,bytes_done=COALESCE(?,bytes_done),error_code=?,error_message=?
                       WHERE id=?""",
                    (str(state), bytes_done, error_code, safe_message, int(file_id)),
                )
                if previous is not None and str(state) == "succeeded":
                    await self._apply_stat_metric_tx(
                        conn,
                        scope_key=f"backup_file:{int(file_id)}",
                        metric="backed_up_bytes",
                        value=int(previous["size_bytes"]),
                        timestamp=time.time(),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

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
