from __future__ import annotations

import aiosqlite


class SQLiteControlRepositoryMixin:
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
