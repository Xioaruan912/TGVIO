from __future__ import annotations

import aiosqlite

from tgvio.domain.control import JobControlState, QueueControlState


class SQLiteControlRepositoryMixin:
    async def request_cancel(self, job_id: str, *, reason: str | None = None) -> None:
        async with self._write_transaction() as conn:
            await self._ensure_control_row(conn, job_id)
            await conn.execute(
                """
                UPDATE job_controls
                SET cancel_requested=1,
                    cancel_reason=?,
                    hold_requested=0,
                    hold_reason=NULL,
                    hold_revision=hold_revision+1,
                    updated_at=CURRENT_TIMESTAMP
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

    async def request_hold(
        self,
        job_id: str,
        *,
        reason: str | None = None,
    ) -> JobControlState:
        async with self._write_transaction() as conn:
            await self._ensure_control_row(conn, job_id)
            await conn.execute(
                """
                UPDATE job_controls
                SET hold_requested=1,
                    hold_reason=?,
                    hold_revision=hold_revision+1,
                    updated_at=CURRENT_TIMESTAMP
                WHERE job_id=?
                """,
                (reason, job_id),
            )
        return await self.get_job_control(job_id)

    async def clear_hold(self, job_id: str) -> JobControlState:
        async with self._write_transaction() as conn:
            await self._ensure_control_row(conn, job_id)
            await conn.execute(
                """
                UPDATE job_controls
                SET hold_requested=0,
                    hold_reason=NULL,
                    hold_revision=hold_revision+1,
                    updated_at=CURRENT_TIMESTAMP
                WHERE job_id=?
                """,
                (job_id,),
            )
        return await self.get_job_control(job_id)

    async def get_job_control(self, job_id: str) -> JobControlState:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT job_id, cancel_requested, cancel_reason, retry_count,
                   hold_requested, hold_reason, hold_revision
            FROM job_controls
            WHERE job_id=?
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            cursor = await conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,))
            job = await cursor.fetchone()
            await cursor.close()
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            return JobControlState(job_id=job_id)
        return JobControlState(
            job_id=str(row["job_id"]),
            cancel_requested=bool(row["cancel_requested"]),
            cancel_reason=row["cancel_reason"],
            retry_count=int(row["retry_count"]),
            hold_requested=bool(row["hold_requested"]),
            hold_reason=row["hold_reason"],
            hold_revision=int(row["hold_revision"]),
        )

    async def set_queue_paused(
        self,
        paused: bool,
        *,
        reason: str | None = None,
    ) -> QueueControlState:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE queue_controls
                SET paused=?, pause_reason=?, revision=revision+1, updated_at=CURRENT_TIMESTAMP
                WHERE singleton=1
                """,
                (1 if paused else 0, reason if paused else None),
            )
        return await self.get_queue_control()

    async def get_queue_control(self) -> QueueControlState:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT paused, pause_reason, revision FROM queue_controls WHERE singleton=1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RuntimeError("queue control singleton is missing")
        return QueueControlState(
            paused=bool(row["paused"]),
            pause_reason=row["pause_reason"],
            revision=int(row["revision"]),
        )

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

    async def get_retry_count(self, job_id: str) -> int:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT retry_count FROM job_controls WHERE job_id=?",
            (job_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            cursor = await conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,))
            job = await cursor.fetchone()
            await cursor.close()
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            return 0
        return int(row["retry_count"])

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
