from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from tgvio.domain.archive import (
    ARCHIVE_OBJECT_TRANSITIONS,
    ARCHIVE_PACKAGE_TRANSITIONS,
    ArchiveEvent,
    ArchiveObject,
    ArchiveObjectRole,
    ArchiveObjectState,
    ArchivePackage,
    ArchivePackageState,
    ArchivePlan,
)
from tgvio.domain.job import ALLOWED_TRANSITIONS, Job, JobEvent, JobState, MediaItem, MediaKind
from tgvio.domain.publish import (
    PublishEffect,
    PublishPlan,
    PublishStep,
    PublishStepKind,
    PublishStepState,
    PublishTarget,
)
from tgvio.domain.progress import JobProgress


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    destination TEXT NOT NULL,
    state TEXT NOT NULL,
    policy_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_owner_state ON jobs(owner_id, state);

CREATE TABLE IF NOT EXISTS job_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    item_index INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    local_path TEXT,
    name TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mime_type TEXT,
    width INTEGER,
    height INTEGER,
    duration_seconds REAL,
    container TEXT,
    codec TEXT,
    spoiler INTEGER NOT NULL DEFAULT 0,
    grouped_id INTEGER,
    source_chat_id INTEGER,
    source_message_id INTEGER,
    sha256 TEXT,
    telegram_ref TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(job_id, item_index)
);

CREATE INDEX IF NOT EXISTS idx_job_items_job ON job_items(job_id, item_index);
CREATE INDEX IF NOT EXISTS idx_job_items_sha256 ON job_items(sha256);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id);

CREATE TABLE IF NOT EXISTS publish_plans (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS publish_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES publish_plans(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    item_indexes_json TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending',
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plan_id, step_index)
);

CREATE INDEX IF NOT EXISTS idx_publish_steps_plan ON publish_steps(plan_id, step_index);
CREATE INDEX IF NOT EXISTS idx_publish_steps_state ON publish_steps(state);

CREATE TABLE IF NOT EXISTS publish_effects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES publish_plans(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    effect_type TEXT NOT NULL,
    external_chat_id TEXT,
    external_message_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_publish_effects_plan ON publish_effects(plan_id, step_index, id);

CREATE TABLE IF NOT EXISTS telegram_file_cache (
    sha256 TEXT NOT NULL,
    destination TEXT NOT NULL,
    media_kind TEXT NOT NULL,
    reference TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(sha256, destination, media_kind)
);

CREATE INDEX IF NOT EXISTS idx_telegram_file_cache_destination
ON telegram_file_cache(destination, media_kind, updated_at);

CREATE TABLE IF NOT EXISTS job_controls (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    cancel_reason TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS archive_packages (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    layout_version TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    staging_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    manifest_sha256 TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    committed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_archive_packages_state
ON archive_packages(state, updated_at);

CREATE TABLE IF NOT EXISTS archive_objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES archive_packages(id) ON DELETE CASCADE,
    object_index INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    local_path TEXT NOT NULL,
    remote_relpath TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    verification_method TEXT,
    remote_etag TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(package_id, object_index),
    UNIQUE(package_id, remote_relpath)
);

CREATE INDEX IF NOT EXISTS idx_archive_objects_state
ON archive_objects(state, next_retry_at, package_id);

CREATE TABLE IF NOT EXISTS archive_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES archive_packages(id) ON DELETE CASCADE,
    object_id INTEGER REFERENCES archive_objects(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_archive_events_package
ON archive_events(package_id, id);

CREATE TABLE IF NOT EXISTS job_progress (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    current_value INTEGER NOT NULL DEFAULT 0,
    total_value INTEGER NOT NULL DEFAULT 0,
    item_index INTEGER,
    item_total INTEGER NOT NULL DEFAULT 0,
    detail_code TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runtime_health (
    component TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class SQLiteJobRepository:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("repository is not open")
        return self._conn

    async def create(self, job: Job) -> None:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO jobs(id, owner_id, destination, state, policy_json, error_code, error_message)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    job.id,
                    job.owner_id,
                    job.destination,
                    job.state.value,
                    json.dumps(job.policy, ensure_ascii=False, separators=(",", ":")),
                    job.error_code,
                    job.error_message,
                ),
            )
            await self._replace_items(conn, job)
            await conn.execute(
                "INSERT OR IGNORE INTO job_controls(job_id) VALUES(?)",
                (job.id,),
            )
            await conn.execute(
                "INSERT INTO job_events(job_id, event_type, from_state, to_state) VALUES(?,?,?,?)",
                (job.id, "job_created", None, job.state.value),
            )
            await conn.execute(
                """
                INSERT INTO job_progress(job_id, phase, current_value, total_value, item_total)
                VALUES(?,?,?,?,?)
                """,
                (job.id, "queued", 0, 0, len(job.items)),
            )

    async def save(self, job: Job) -> None:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE jobs
                SET owner_id=?, destination=?, state=?, policy_json=?, error_code=?, error_message=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    job.owner_id,
                    job.destination,
                    job.state.value,
                    json.dumps(job.policy, ensure_ascii=False, separators=(",", ":")),
                    job.error_code,
                    job.error_message,
                    job.id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"job not found: {job.id}")
            await self._replace_items(conn, job)

    async def transition(
        self,
        job_id: str,
        next_state: JobState,
        *,
        event_type: str,
        detail: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Job:
        async with self._write_transaction() as conn:
            cursor = await conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(f"job not found: {job_id}")
            current = JobState(row["state"])
            if next_state not in ALLOWED_TRANSITIONS[current]:
                raise ValueError(f"illegal transition: {current.value} -> {next_state.value}")
            await conn.execute(
                """
                UPDATE jobs
                SET state=?, error_code=?, error_message=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (next_state.value, error_code, error_message, job_id),
            )
            await conn.execute(
                """
                INSERT INTO job_events(job_id, event_type, from_state, to_state, detail)
                VALUES(?,?,?,?,?)
                """,
                (job_id, event_type, current.value, next_state.value, detail),
            )
        job = await self.get(job_id)
        if job is None:
            raise RuntimeError("job disappeared after transition")
        return job

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            conn = self._require()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def get(self, job_id: str) -> Job | None:
        conn = self._require()
        cursor = await conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        items = await self.list_items(job_id)
        return Job(
            id=row["id"],
            owner_id=int(row["owner_id"]),
            destination=str(row["destination"]),
            state=JobState(row["state"]),
            policy=json.loads(row["policy_json"] or "{}"),
            items=items,
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_items(self, job_id: str) -> list[MediaItem]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM job_items WHERE job_id=? ORDER BY item_index",
            (job_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._item_from_row(row) for row in rows]

    async def list_events(self, job_id: str) -> list[JobEvent]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY id",
            (job_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            JobEvent(
                id=int(row["id"]),
                job_id=row["job_id"],
                event_type=row["event_type"],
                from_state=JobState(row["from_state"]) if row["from_state"] else None,
                to_state=JobState(row["to_state"]),
                detail=row["detail"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def list_by_states(self, states: tuple[JobState, ...]) -> list[Job]:
        if not states:
            return []
        conn = self._require()
        placeholders = ",".join("?" for _ in states)
        cursor = await conn.execute(
            f"SELECT id FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at, id",
            tuple(state.value for state in states),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        jobs: list[Job] = []
        for row in rows:
            job = await self.get(row["id"])
            if job is not None:
                jobs.append(job)
        return jobs

    async def list_recent(self, *, owner_id: int | None = None, limit: int = 10) -> list[Job]:
        if limit <= 0:
            return []
        conn = self._require()
        if owner_id is None:
            cursor = await conn.execute(
                "SELECT id FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        else:
            cursor = await conn.execute(
                "SELECT id FROM jobs WHERE owner_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (owner_id, limit),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        jobs: list[Job] = []
        for row in rows:
            job = await self.get(row["id"])
            if job is not None:
                jobs.append(job)
        return jobs

    async def count_by_state(self, *, owner_id: int | None = None) -> dict[JobState, int]:
        conn = self._require()
        if owner_id is None:
            cursor = await conn.execute("SELECT state, COUNT(*) AS count FROM jobs GROUP BY state")
        else:
            cursor = await conn.execute(
                "SELECT state, COUNT(*) AS count FROM jobs WHERE owner_id=? GROUP BY state",
                (owner_id,),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return {JobState(row["state"]): int(row["count"]) for row in rows}

    async def get_stats_snapshot(self, *, owner_id: int | None = None) -> dict[str, int]:
        conn = self._require()
        owner_clause = "" if owner_id is None else "WHERE j.owner_id=?"
        params: tuple[object, ...] = () if owner_id is None else (owner_id,)
        cursor = await conn.execute(
            f"""
            SELECT
                COUNT(DISTINCT j.id) AS jobs_total,
                COUNT(ji.id) AS media_items,
                COALESCE(SUM(ji.size_bytes), 0) AS media_bytes,
                COUNT(DISTINCT CASE WHEN j.state='succeeded' THEN j.id END) AS succeeded,
                COUNT(DISTINCT CASE WHEN j.state='failed' THEN j.id END) AS failed,
                COUNT(DISTINCT CASE WHEN j.state='cancelled' THEN j.id END) AS cancelled,
                COUNT(DISTINCT CASE WHEN date(j.created_at)=date('now') THEN j.id END) AS today_jobs,
                COUNT(DISTINCT CASE WHEN date(j.updated_at)=date('now') AND j.state='succeeded' THEN j.id END) AS today_succeeded,
                COUNT(DISTINCT CASE WHEN date(j.updated_at)=date('now') AND j.state='failed' THEN j.id END) AS today_failed,
                COUNT(DISTINCT CASE WHEN date(j.updated_at)=date('now') AND j.state='cancelled' THEN j.id END) AS today_cancelled
            FROM jobs j
            LEFT JOIN job_items ji ON ji.job_id=j.id
            {owner_clause}
            """,
            params,
        )
        row = await cursor.fetchone()
        await cursor.close()
        snapshot = {
            key: int(row[key] or 0)
            for key in (
                "jobs_total",
                "media_items",
                "media_bytes",
                "succeeded",
                "failed",
                "cancelled",
                "today_jobs",
                "today_succeeded",
                "today_failed",
                "today_cancelled",
            )
        }
        if owner_id is None:
            cursor = await conn.execute("SELECT COUNT(*) AS count FROM telegram_file_cache")
            cache_row = await cursor.fetchone()
            await cursor.close()
            snapshot["telegram_cache_entries"] = int(cache_row["count"] or 0)
        return snapshot

    async def get_recent_event_counts(
        self,
        *,
        owner_id: int | None = None,
        hours: int = 24,
    ) -> dict[str, int]:
        conn = self._require()
        hours = min(max(int(hours), 1), 24 * 30)
        if owner_id is None:
            cursor = await conn.execute(
                """
                SELECT e.event_type, COUNT(*) AS count
                FROM job_events e
                WHERE e.created_at >= datetime('now', ?)
                GROUP BY e.event_type
                ORDER BY count DESC, e.event_type
                """,
                (f"-{hours} hours",),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT e.event_type, COUNT(*) AS count
                FROM job_events e
                JOIN jobs j ON j.id=e.job_id
                WHERE j.owner_id=? AND e.created_at >= datetime('now', ?)
                GROUP BY e.event_type
                ORDER BY count DESC, e.event_type
                """,
                (owner_id, f"-{hours} hours"),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row["event_type"]): int(row["count"]) for row in rows}

    async def quick_check(self) -> bool:
        conn = self._require()
        cursor = await conn.execute("PRAGMA quick_check")
        row = await cursor.fetchone()
        await cursor.close()
        return bool(row and row[0] == "ok")

    async def set_job_progress(self, progress: JobProgress) -> None:
        async with self._write_transaction() as conn:
            cursor = await conn.execute("SELECT 1 FROM jobs WHERE id=?", (progress.job_id,))
            exists = await cursor.fetchone()
            await cursor.close()
            if exists is None:
                raise KeyError(f"job not found: {progress.job_id}")
            await conn.execute(
                """
                INSERT INTO job_progress(
                    job_id, phase, current_value, total_value,
                    item_index, item_total, detail_code, updated_at
                ) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(job_id)
                DO UPDATE SET
                    phase=excluded.phase,
                    current_value=excluded.current_value,
                    total_value=excluded.total_value,
                    item_index=excluded.item_index,
                    item_total=excluded.item_total,
                    detail_code=excluded.detail_code,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    progress.job_id,
                    progress.phase,
                    progress.current,
                    progress.total,
                    progress.item_index,
                    progress.item_total,
                    progress.detail_code,
                ),
            )

    async def get_job_progress(self, job_id: str) -> JobProgress | None:
        conn = self._require()
        cursor = await conn.execute("SELECT * FROM job_progress WHERE job_id=?", (job_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return JobProgress(
            job_id=str(row["job_id"]),
            phase=str(row["phase"]),
            current=int(row["current_value"]),
            total=int(row["total_value"]),
            item_index=int(row["item_index"]) if row["item_index"] is not None else None,
            item_total=int(row["item_total"]),
            detail_code=row["detail_code"],
            updated_at=row["updated_at"],
        )

    async def save_publish_plan(self, plan: PublishPlan) -> None:
        async with self._write_transaction() as conn:
            cursor = await conn.execute("SELECT 1 FROM jobs WHERE id=?", (plan.job_id,))
            exists = await cursor.fetchone()
            await cursor.close()
            if exists is None:
                raise KeyError(f"job not found: {plan.job_id}")
            await conn.execute("DELETE FROM publish_plans WHERE job_id=?", (plan.job_id,))
            await conn.execute(
                "INSERT INTO publish_plans(id, job_id, version, summary_json) VALUES(?,?,?,?)",
                (
                    plan.id,
                    plan.job_id,
                    plan.version,
                    json.dumps(plan.summary, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            for step in plan.steps:
                await conn.execute(
                    """
                    INSERT INTO publish_steps(
                        plan_id, step_index, kind, target, item_indexes_json,
                        params_json, state, error_code, error_message
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        plan.id,
                        step.index,
                        step.kind.value,
                        step.target.value,
                        json.dumps(list(step.item_indexes), separators=(",", ":")),
                        json.dumps(step.params, ensure_ascii=False, separators=(",", ":")),
                        step.state.value,
                        step.error_code,
                        step.error_message,
                    ),
                )

    async def get_publish_plan(self, job_id: str) -> PublishPlan | None:
        conn = self._require()
        cursor = await conn.execute("SELECT * FROM publish_plans WHERE job_id=?", (job_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        cursor = await conn.execute(
            "SELECT * FROM publish_steps WHERE plan_id=? ORDER BY step_index",
            (row["id"],),
        )
        step_rows = await cursor.fetchall()
        await cursor.close()
        steps = tuple(self._publish_step_from_row(step_row) for step_row in step_rows)
        return PublishPlan(
            id=row["id"],
            job_id=row["job_id"],
            version=int(row["version"]),
            summary=json.loads(row["summary_json"] or "{}"),
            steps=steps,
            created_at=row["created_at"],
        )

    async def update_publish_step_state(
        self,
        plan_id: str,
        step_index: int,
        state: PublishStepState,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> PublishStep:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE publish_steps
                SET state=?, error_code=?, error_message=?, updated_at=CURRENT_TIMESTAMP
                WHERE plan_id=? AND step_index=?
                """,
                (state.value, error_code, error_message, plan_id, step_index),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"publish step not found: {plan_id}:{step_index}")
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM publish_steps WHERE plan_id=? AND step_index=?",
            (plan_id, step_index),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RuntimeError("publish step disappeared after update")
        return self._publish_step_from_row(row)

    async def record_publish_effect(self, effect: PublishEffect) -> PublishEffect:
        recorded = await self.record_publish_effects((effect,))
        return recorded[0]

    async def record_publish_effects(
        self,
        effects: tuple[PublishEffect, ...],
    ) -> list[PublishEffect]:
        if not effects:
            return []
        effect_ids: list[int] = []
        async with self._write_transaction() as conn:
            for effect in effects:
                cursor = await conn.execute(
                    """
                    INSERT INTO publish_effects(
                        plan_id, step_index, effect_type, external_chat_id,
                        external_message_id, detail_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        effect.plan_id,
                        effect.step_index,
                        effect.effect_type,
                        effect.external_chat_id,
                        effect.external_message_id,
                        json.dumps(effect.detail, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                effect_ids.append(int(cursor.lastrowid))
        conn = self._require()
        placeholders = ",".join("?" for _ in effect_ids)
        cursor = await conn.execute(
            f"SELECT * FROM publish_effects WHERE id IN ({placeholders}) ORDER BY id",
            tuple(effect_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        if len(rows) != len(effect_ids):
            raise RuntimeError("publish effects disappeared after insert")
        return [self._publish_effect_from_row(row) for row in rows]

    async def list_publish_effects(self, plan_id: str) -> list[PublishEffect]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM publish_effects WHERE plan_id=? ORDER BY step_index, id",
            (plan_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._publish_effect_from_row(row) for row in rows]

    async def get_telegram_reference(
        self,
        sha256: str,
        destination: str,
        media_kind: MediaKind,
    ) -> str | None:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT reference
            FROM telegram_file_cache
            WHERE sha256=? AND destination=? AND media_kind=?
            """,
            (sha256, destination, media_kind.value),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return str(row["reference"]) if row is not None else None

    async def upsert_telegram_reference(
        self,
        sha256: str,
        destination: str,
        media_kind: MediaKind,
        reference: str,
    ) -> None:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO telegram_file_cache(
                    sha256, destination, media_kind, reference
                ) VALUES(?,?,?,?)
                ON CONFLICT(sha256, destination, media_kind)
                DO UPDATE SET
                    reference=excluded.reference,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (sha256, destination, media_kind.value, reference),
            )

    async def set_runtime_health(
        self,
        component: str,
        status: str,
        *,
        detail: dict[str, object] | None = None,
    ) -> None:
        component = component.strip()
        status = status.strip()
        if not component or not status:
            raise ValueError("runtime health component/status must be non-empty")
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO runtime_health(component, status, detail_json, updated_at)
                VALUES(?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(component)
                DO UPDATE SET
                    status=excluded.status,
                    detail_json=excluded.detail_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    component,
                    status,
                    json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    async def get_runtime_health(self) -> dict[str, dict[str, object]]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT component, status, detail_json, updated_at FROM runtime_health ORDER BY component"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            str(row["component"]): {
                "status": str(row["status"]),
                "detail": json.loads(row["detail_json"] or "{}"),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        }

    async def request_cancel(self, job_id: str, *, reason: str | None = None) -> None:
        async with self._write_transaction() as conn:
            await self._ensure_control_row(conn, job_id)
            await conn.execute(
                """
                UPDATE job_controls
                SET cancel_requested=1, cancel_reason=?, updated_at=CURRENT_TIMESTAMP
                WHERE job_id=?
                """,
                (reason, job_id),
            )

    async def clear_cancel_request(self, job_id: str) -> None:
        async with self._write_transaction() as conn:
            await self._ensure_control_row(conn, job_id)
            await conn.execute(
                """
                UPDATE job_controls
                SET cancel_requested=0, cancel_reason=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE job_id=?
                """,
                (job_id,),
            )

    async def is_cancel_requested(self, job_id: str) -> bool:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT cancel_requested FROM job_controls WHERE job_id=?",
            (job_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return bool(row and row["cancel_requested"])

    async def increment_retry_count(self, job_id: str) -> int:
        async with self._write_transaction() as conn:
            await self._ensure_control_row(conn, job_id)
            await conn.execute(
                """
                UPDATE job_controls
                SET retry_count=retry_count+1, updated_at=CURRENT_TIMESTAMP
                WHERE job_id=?
                """,
                (job_id,),
            )
            cursor = await conn.execute(
                "SELECT retry_count FROM job_controls WHERE job_id=?",
                (job_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise RuntimeError(f"job control disappeared: {job_id}")
        return int(row["retry_count"])

    async def save_archive_plan(self, plan: ArchivePlan) -> ArchivePackage:
        package = plan.package
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT state FROM archive_packages WHERE job_id=?",
                (package.job_id,),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is not None and existing["state"] != ArchivePackageState.PLANNED.value:
                raise ValueError(
                    f"archive package cannot be replanned from state {existing['state']}"
                )
            if existing is not None:
                await conn.execute(
                    "DELETE FROM archive_packages WHERE job_id=?",
                    (package.job_id,),
                )
            await conn.execute(
                """
                INSERT INTO archive_packages(
                    id, job_id, layout_version, remote_path, staging_path,
                    state, manifest_json, manifest_sha256, error_code, error_message
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    package.id,
                    package.job_id,
                    package.layout_version,
                    package.remote_path,
                    package.staging_path,
                    package.state.value,
                    json.dumps(package.manifest, ensure_ascii=False, separators=(",", ":")),
                    package.manifest_sha256,
                    package.error_code,
                    package.error_message,
                ),
            )
            for obj in package.objects:
                await conn.execute(
                    """
                    INSERT INTO archive_objects(
                        package_id, object_index, item_index, role, local_path,
                        remote_relpath, size_bytes, sha256, state,
                        verification_method, remote_etag, retry_count,
                        next_retry_at, error_code, error_message
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        package.id,
                        obj.object_index,
                        obj.item_index,
                        obj.role.value,
                        obj.local_path,
                        obj.remote_relpath,
                        obj.size_bytes,
                        obj.sha256,
                        obj.state.value,
                        obj.verification_method,
                        obj.remote_etag,
                        obj.retry_count,
                        obj.next_retry_at,
                        obj.error_code,
                        obj.error_message,
                    ),
                )
            await conn.execute(
                """
                INSERT INTO archive_events(package_id, event_type, detail_json)
                VALUES(?,?,?)
                """,
                (
                    package.id,
                    "archive_planned",
                    json.dumps(plan.summary, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        loaded = await self.get_archive_package_for_job(package.job_id)
        if loaded is None:
            raise RuntimeError("archive package disappeared after save")
        return loaded

    async def get_archive_package_for_job(self, job_id: str) -> ArchivePackage | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM archive_packages WHERE job_id=?",
            (job_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        cursor = await conn.execute(
            "SELECT * FROM archive_objects WHERE package_id=? ORDER BY object_index",
            (row["id"],),
        )
        object_rows = await cursor.fetchall()
        await cursor.close()
        return self._archive_package_from_rows(row, object_rows)

    async def get_archive_package(self, package_id: str) -> ArchivePackage | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM archive_packages WHERE id=?",
            (package_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        cursor = await conn.execute(
            "SELECT * FROM archive_objects WHERE package_id=? ORDER BY object_index",
            (package_id,),
        )
        object_rows = await cursor.fetchall()
        await cursor.close()
        return self._archive_package_from_rows(row, object_rows)

    async def list_archive_packages_by_states(
        self,
        states: tuple[ArchivePackageState, ...],
        *,
        limit: int = 20,
    ) -> list[ArchivePackage]:
        if not states:
            return []
        conn = self._require()
        placeholders = ",".join("?" for _ in states)
        cursor = await conn.execute(
            f"""
            SELECT id FROM archive_packages
            WHERE state IN ({placeholders})
            ORDER BY created_at, id
            LIMIT ?
            """,
            (*tuple(state.value for state in states), max(1, min(100, int(limit)))),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        packages: list[ArchivePackage] = []
        for row in rows:
            package = await self.get_archive_package(str(row["id"]))
            if package is not None:
                packages.append(package)
        return packages

    async def count_archive_packages_by_state(
        self,
        *,
        owner_id: int | None = None,
    ) -> dict[ArchivePackageState, int]:
        conn = self._require()
        if owner_id is None:
            cursor = await conn.execute(
                "SELECT state, COUNT(*) AS count FROM archive_packages GROUP BY state"
            )
        else:
            cursor = await conn.execute(
                """
                SELECT a.state, COUNT(*) AS count
                FROM archive_packages a
                JOIN jobs j ON j.id=a.job_id
                WHERE j.owner_id=?
                GROUP BY a.state
                """,
                (owner_id,),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            ArchivePackageState(row["state"]): int(row["count"])
            for row in rows
        }

    async def list_recent_archive_packages(
        self,
        *,
        owner_id: int | None = None,
        limit: int = 5,
    ) -> list[ArchivePackage]:
        conn = self._require()
        bounded = max(1, min(50, int(limit)))
        if owner_id is None:
            cursor = await conn.execute(
                "SELECT id FROM archive_packages ORDER BY created_at DESC, id DESC LIMIT ?",
                (bounded,),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT a.id
                FROM archive_packages a
                JOIN jobs j ON j.id=a.job_id
                WHERE j.owner_id=?
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT ?
                """,
                (owner_id, bounded),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        packages: list[ArchivePackage] = []
        for row in rows:
            package = await self.get_archive_package(str(row["id"]))
            if package is not None:
                packages.append(package)
        return packages

    async def list_archive_events(self, package_id: str) -> list[ArchiveEvent]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM archive_events WHERE package_id=? ORDER BY id",
            (package_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            ArchiveEvent(
                id=int(row["id"]),
                package_id=row["package_id"],
                object_id=int(row["object_id"]) if row["object_id"] is not None else None,
                event_type=row["event_type"],
                detail=json.loads(row["detail_json"] or "{}"),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def update_archive_package_state(
        self,
        package_id: str,
        state: ArchivePackageState,
        *,
        event_type: str,
        detail: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        committed: bool = False,
    ) -> ArchivePackage:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT state FROM archive_packages WHERE id=?",
                (package_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(f"archive package not found: {package_id}")
            current = ArchivePackageState(row["state"])
            if state != current and state not in ARCHIVE_PACKAGE_TRANSITIONS[current]:
                raise ValueError(
                    f"illegal archive package transition: {current.value} -> {state.value}"
                )
            await conn.execute(
                """
                UPDATE archive_packages
                SET state=?, error_code=?, error_message=?,
                    committed_at=CASE
                        WHEN ? THEN COALESCE(committed_at, CURRENT_TIMESTAMP)
                        ELSE committed_at
                    END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    state.value,
                    error_code,
                    error_message,
                    int(committed),
                    package_id,
                ),
            )
            await conn.execute(
                """
                INSERT INTO archive_events(package_id, event_type, detail_json)
                VALUES(?,?,?)
                """,
                (
                    package_id,
                    event_type,
                    json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        loaded = await self.get_archive_package(package_id)
        if loaded is None:
            raise RuntimeError("archive package disappeared after state update")
        return loaded

    async def update_archive_object_state(
        self,
        object_id: int,
        state: ArchiveObjectState,
        *,
        event_type: str,
        verification_method: str | None = None,
        remote_etag: str | None = None,
        retry_count: int | None = None,
        next_retry_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> ArchiveObject:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM archive_objects WHERE id=?",
                (object_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(f"archive object not found: {object_id}")
            current = ArchiveObjectState(row["state"])
            if state != current and state not in ARCHIVE_OBJECT_TRANSITIONS[current]:
                raise ValueError(
                    f"illegal archive object transition: {current.value} -> {state.value}"
                )
            await conn.execute(
                """
                UPDATE archive_objects
                SET state=?, verification_method=COALESCE(?, verification_method),
                    remote_etag=COALESCE(?, remote_etag),
                    retry_count=COALESCE(?, retry_count), next_retry_at=?,
                    error_code=?, error_message=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    state.value,
                    verification_method,
                    remote_etag,
                    retry_count,
                    next_retry_at,
                    error_code,
                    error_message,
                    object_id,
                ),
            )
            await conn.execute(
                """
                INSERT INTO archive_events(package_id, object_id, event_type, detail_json)
                VALUES(?,?,?,?)
                """,
                (
                    str(row["package_id"]),
                    object_id,
                    event_type,
                    json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            cursor = await conn.execute(
                "SELECT * FROM archive_objects WHERE id=?",
                (object_id,),
            )
            updated = await cursor.fetchone()
            await cursor.close()
        if updated is None:
            raise RuntimeError("archive object disappeared after state update")
        return self._archive_object_from_row(updated)

    async def _ensure_control_row(self, conn: aiosqlite.Connection, job_id: str) -> None:
        cursor = await conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        await conn.execute(
            "INSERT OR IGNORE INTO job_controls(job_id) VALUES(?)",
            (job_id,),
        )

    @staticmethod
    def _archive_package_from_rows(
        row: aiosqlite.Row,
        object_rows: list[aiosqlite.Row],
    ) -> ArchivePackage:
        objects = tuple(SQLiteJobRepository._archive_object_from_row(obj) for obj in object_rows)
        return ArchivePackage(
            id=str(row["id"]),
            job_id=str(row["job_id"]),
            layout_version=str(row["layout_version"]),
            remote_path=str(row["remote_path"]),
            staging_path=str(row["staging_path"]),
            state=ArchivePackageState(row["state"]),
            manifest=json.loads(row["manifest_json"] or "{}"),
            objects=objects,
            manifest_sha256=row["manifest_sha256"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            committed_at=row["committed_at"],
        )

    @staticmethod
    def _archive_object_from_row(row: aiosqlite.Row) -> ArchiveObject:
        return ArchiveObject(
            id=int(row["id"]),
            package_id=str(row["package_id"]),
            object_index=int(row["object_index"]),
            item_index=int(row["item_index"]),
            role=ArchiveObjectRole(row["role"]),
            local_path=str(row["local_path"]),
            remote_relpath=str(row["remote_relpath"]),
            size_bytes=int(row["size_bytes"]),
            sha256=str(row["sha256"]),
            state=ArchiveObjectState(row["state"]),
            verification_method=row["verification_method"],
            remote_etag=row["remote_etag"],
            retry_count=int(row["retry_count"]),
            next_retry_at=row["next_retry_at"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            updated_at=row["updated_at"],
        )

    async def _replace_items(self, conn: aiosqlite.Connection, job: Job) -> None:
        await conn.execute("DELETE FROM job_items WHERE job_id=?", (job.id,))
        for item in job.items:
            await conn.execute(
                """
                INSERT INTO job_items(
                    job_id, item_index, kind, source, caption, local_path, name,
                    size_bytes, mime_type, width, height, duration_seconds,
                    container, codec, spoiler, grouped_id, source_chat_id,
                    source_message_id, sha256, telegram_ref, metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.id,
                    item.index,
                    item.kind.value,
                    item.source,
                    item.caption,
                    item.local_path,
                    item.name,
                    item.size_bytes,
                    item.mime_type,
                    item.width,
                    item.height,
                    item.duration_seconds,
                    item.container,
                    item.codec,
                    int(item.spoiler),
                    item.grouped_id,
                    item.source_chat_id,
                    item.source_message_id,
                    item.sha256,
                    item.telegram_ref,
                    json.dumps(item.metadata, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    @staticmethod
    def _item_from_row(row: aiosqlite.Row) -> MediaItem:
        return MediaItem(
            index=int(row["item_index"]),
            kind=MediaKind(row["kind"]),
            source=row["source"],
            caption=row["caption"],
            local_path=row["local_path"],
            name=row["name"],
            size_bytes=int(row["size_bytes"]),
            mime_type=row["mime_type"],
            width=row["width"],
            height=row["height"],
            duration_seconds=row["duration_seconds"],
            container=row["container"],
            codec=row["codec"],
            spoiler=bool(row["spoiler"]),
            grouped_id=row["grouped_id"],
            source_chat_id=row["source_chat_id"],
            source_message_id=row["source_message_id"],
            sha256=row["sha256"],
            telegram_ref=row["telegram_ref"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    @staticmethod
    def _publish_step_from_row(row: aiosqlite.Row) -> PublishStep:
        return PublishStep(
            index=int(row["step_index"]),
            kind=PublishStepKind(row["kind"]),
            target=PublishTarget(row["target"]),
            item_indexes=tuple(int(value) for value in json.loads(row["item_indexes_json"] or "[]")),
            params=json.loads(row["params_json"] or "{}"),
            state=PublishStepState(row["state"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    @staticmethod
    def _publish_effect_from_row(row: aiosqlite.Row) -> PublishEffect:
        return PublishEffect(
            id=int(row["id"]),
            plan_id=row["plan_id"],
            step_index=int(row["step_index"]),
            effect_type=row["effect_type"],
            external_chat_id=row["external_chat_id"],
            external_message_id=row["external_message_id"],
            detail=json.loads(row["detail_json"] or "{}"),
            created_at=row["created_at"],
        )
