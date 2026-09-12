from __future__ import annotations

import json

import aiosqlite

from tgvio.domain.job import ALLOWED_TRANSITIONS, Job, JobEvent, JobState, MediaItem, MediaKind


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
