from __future__ import annotations

import json

import aiosqlite

from tgvio.domain.job import ALLOWED_TRANSITIONS, Job, JobEvent, JobState, MediaItem, MediaKind
from tgvio.domain.job_query import (
    FailurePage,
    FailureSummary,
    JobListEntry,
    JobListFilter,
    JobPage,
)


class SQLiteJobRepositoryMixin:
    async def create(self, job: Job) -> None:
        async with self._write_transaction() as conn:
            await self._insert_new_job(conn, job)

    async def _insert_new_job(self, conn: aiosqlite.Connection, job: Job) -> None:
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
            "INSERT INTO job_schedule(job_id) VALUES(?)",
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

    async def update_job_policy(
        self,
        job_id: str,
        updates: dict[str, object],
        *,
        remove_keys: tuple[str, ...] = (),
    ) -> Job:
        """Atomically patch top-level policy fields without replacing Job items/state."""

        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT policy_json FROM jobs WHERE id=?",
                (job_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(f"job not found: {job_id}")
            policy = json.loads(row["policy_json"] or "{}")
            if not isinstance(policy, dict):
                raise ValueError(f"job policy must be an object: {job_id}")
            for key in remove_keys:
                policy.pop(str(key), None)
            policy.update({str(key): value for key, value in updates.items()})
            await conn.execute(
                """
                UPDATE jobs
                SET policy_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    json.dumps(policy, ensure_ascii=False, separators=(",", ":")),
                    job_id,
                ),
            )
        job = await self.get(job_id)
        if job is None:
            raise RuntimeError("job disappeared after policy update")
        return job

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

    async def get_by_accepted_order(
        self,
        owner_id: int,
        accepted_order: int,
    ) -> Job | None:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT s.job_id
            FROM job_schedule s
            JOIN jobs j ON j.id=s.job_id
            WHERE j.owner_id=? AND s.accepted_order=?
            """,
            (int(owner_id), int(accepted_order)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return await self.get(str(row["job_id"]))

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

    async def list_failed_jobs_for_auto_recovery(
        self,
        *,
        limit: int = 100,
    ) -> list[Job]:
        """Return only policy-managed failures still needing a recovery decision."""

        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT j.id
            FROM jobs j
            WHERE j.state='failed'
              AND json_extract(j.policy_json, '$.auto_recovery.version')=1
              AND json_extract(j.policy_json, '$.auto_recovery.enabled')=1
              AND (
                    COALESCE(
                        json_extract(j.policy_json, '$.auto_recovery_job.status'),
                        ''
                    ) NOT IN ('abandoned','exhausted','manual_review','quarantined')
                    OR COALESCE(
                        json_extract(j.policy_json, '$.auto_recovery_job.failure_id'),
                        ''
                    ) != ('event:' || COALESCE(
                        (
                            SELECT MAX(e.id)
                            FROM job_events e
                            WHERE e.job_id=j.id
                              AND e.to_state='failed'
                              AND COALESCE(e.from_state, '')!='failed'
                        ),
                        ''
                    ))
              )
            ORDER BY j.updated_at, j.id
            LIMIT ?
            """,
            (max(1, min(500, int(limit))),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        jobs: list[Job] = []
        for row in rows:
            job = await self.get(str(row["id"]))
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

    async def page_jobs(
        self,
        *,
        owner_id: int,
        filter: JobListFilter = JobListFilter.ALL,
        page: int = 0,
        page_size: int = 5,
    ) -> JobPage:
        filter = JobListFilter(filter)
        size = max(1, min(20, int(page_size)))
        requested_page = max(0, int(page))
        conditions = ["j.owner_id=?"]
        params: list[object] = [int(owner_id)]
        if filter == JobListFilter.ACTIVE:
            conditions.extend(
                [
                    "j.state NOT IN ('succeeded','failed','cancelled')",
                    "COALESCE(c.hold_requested,0)=0",
                ]
            )
        elif filter == JobListFilter.HELD:
            conditions.extend(
                [
                    "j.state NOT IN ('succeeded','failed','cancelled')",
                    "COALESCE(c.hold_requested,0)=1",
                ]
            )
        elif filter == JobListFilter.FAILED:
            conditions.append("j.state='failed'")
        elif filter == JobListFilter.COMPLETED:
            conditions.append("j.state IN ('succeeded','cancelled')")
        where = " AND ".join(conditions)
        conn = self._require()
        cursor = await conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM jobs j
            LEFT JOIN job_controls c ON c.job_id=j.id
            WHERE {where}
            """,
            tuple(params),
        )
        total_row = await cursor.fetchone()
        await cursor.close()
        total = int(total_row["count"]) if total_row is not None else 0
        total_pages = max(1, (total + size - 1) // size)
        current_page = min(requested_page, total_pages - 1)
        cursor = await conn.execute(
            f"""
            SELECT
                j.id,
                COALESCE(c.hold_requested,0) AS held,
                s.accepted_order AS accepted_order
            FROM jobs j
            LEFT JOIN job_controls c ON c.job_id=j.id
            LEFT JOIN job_schedule s ON s.job_id=j.id
            WHERE {where}
            ORDER BY j.created_at DESC, j.rowid DESC
            LIMIT ? OFFSET ?
            """,
            (*params, size, current_page * size),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        entries: list[JobListEntry] = []
        for row in rows:
            job = await self.get(str(row["id"]))
            if job is not None:
                entries.append(
                    JobListEntry(
                        job=job,
                        held=bool(row["held"]) and not job.terminal,
                        accepted_order=(
                            None
                            if row["accepted_order"] is None
                            else int(row["accepted_order"])
                        ),
                    )
                )
        return JobPage(
            entries=tuple(entries),
            filter=filter,
            page=current_page,
            page_size=size,
            total=total,
        )

    async def page_failures(
        self,
        *,
        owner_id: int,
        page: int = 0,
        page_size: int = 5,
    ) -> FailurePage:
        size = max(1, min(20, int(page_size)))
        requested_page = max(0, int(page))
        managed = """
            COALESCE(json_extract(j.policy_json, '$.auto_recovery.version'),0)=1
            AND COALESCE(json_extract(j.policy_json, '$.auto_recovery.enabled'),0)=1
        """
        job_actionable = f"""
            j.state='failed'
            AND NOT (
                {managed}
                AND COALESCE(
                    json_extract(j.policy_json, '$.auto_recovery_job.status'),
                    ''
                ) NOT IN ('abandoned','exhausted','manual_review','quarantined')
            )
        """
        archive_actionable = f"""
            a.state='failed'
            AND NOT (
                {managed}
                AND COALESCE(
                    json_extract(j.policy_json, '$.auto_recovery_archive.status'),
                    ''
                ) NOT IN ('abandoned','exhausted')
            )
        """
        conn = self._require()
        cursor = await conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM jobs j
            LEFT JOIN archive_packages a ON a.job_id=j.id
            WHERE j.owner_id=?
              AND (({job_actionable}) OR ({archive_actionable}))
            """,
            (int(owner_id),),
        )
        total_row = await cursor.fetchone()
        await cursor.close()
        total = int(total_row["count"]) if total_row is not None else 0
        total_pages = max(1, (total + size - 1) // size)
        current_page = min(requested_page, total_pages - 1)
        cursor = await conn.execute(
            f"""
            SELECT
                j.id,
                CASE WHEN ({job_actionable}) THEN 1 ELSE 0 END AS job_actionable,
                CASE WHEN ({archive_actionable}) THEN 1 ELSE 0 END AS archive_actionable,
                a.id AS archive_package_id,
                a.error_code AS archive_error_code,
                s.accepted_order AS accepted_order
            FROM jobs j
            LEFT JOIN archive_packages a ON a.job_id=j.id
            LEFT JOIN job_schedule s ON s.job_id=j.id
            WHERE j.owner_id=?
              AND (({job_actionable}) OR ({archive_actionable}))
            ORDER BY
                CASE
                    WHEN j.state='failed' AND j.error_code IN ('publish_partial','publish_uncertain') THEN 0
                    WHEN j.state='failed' THEN 1
                    ELSE 2
                END,
                j.updated_at DESC,
                j.rowid DESC
            LIMIT ? OFFSET ?
            """,
            (int(owner_id), size, current_page * size),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        entries: list[FailureSummary] = []
        for row in rows:
            job = await self.get(str(row["id"]))
            if job is None:
                continue
            entries.append(
                FailureSummary(
                    job=job,
                    job_actionable=bool(row["job_actionable"]),
                    archive_actionable=bool(row["archive_actionable"]),
                    archive_package_id=row["archive_package_id"],
                    archive_error_code=row["archive_error_code"],
                    accepted_order=(
                        None
                        if row["accepted_order"] is None
                        else int(row["accepted_order"])
                    ),
                )
            )
        return FailurePage(
            entries=tuple(entries),
            page=current_page,
            page_size=size,
            total=total,
        )

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
