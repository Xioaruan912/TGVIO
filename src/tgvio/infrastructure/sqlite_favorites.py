from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from tgvio.domain.job import Job


class SQLiteFavoritesRepositoryMixin:
    async def add_favorite(self, owner_id: int, job_id: str) -> bool:
        job = await self.get(str(job_id))
        if job is None or int(job.owner_id) != int(owner_id):
            return False
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO favorites(owner_id, job_id) VALUES(?,?)",
                (int(owner_id), str(job_id)),
            )
            return bool(cursor.rowcount and cursor.rowcount > 0)

    async def remove_favorite(self, owner_id: int, job_id: str) -> bool:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM favorites WHERE owner_id=? AND job_id=?",
                (int(owner_id), str(job_id)),
            )
            return bool(cursor.rowcount and cursor.rowcount > 0)

    async def is_favorite(self, owner_id: int, job_id: str) -> bool:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT 1 FROM favorites WHERE owner_id=? AND job_id=?",
            (int(owner_id), str(job_id)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def favorite_job_ids(
        self,
        owner_id: int,
        job_ids: tuple[str, ...],
    ) -> set[str]:
        if not job_ids:
            return set()
        placeholders = ",".join("?" for _ in job_ids)
        conn = self._require()
        cursor = await conn.execute(
            f"""
            SELECT job_id FROM favorites
            WHERE owner_id=? AND job_id IN ({placeholders})
            """,
            (int(owner_id), *(str(value) for value in job_ids)),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row["job_id"]) for row in rows}

    async def count_favorites(self, owner_id: int) -> int:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT COUNT(*) AS count FROM favorites WHERE owner_id=?",
            (int(owner_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["count"]) if row is not None else 0

    async def list_favorite_ids(
        self,
        owner_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[str]:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT f.job_id AS job_id
            FROM favorites f
            JOIN jobs j ON j.id=f.job_id
            WHERE f.owner_id=?
            ORDER BY f.created_at DESC, f.job_id
            LIMIT ? OFFSET ?
            """,
            (int(owner_id), max(1, int(limit)), max(0, int(offset))),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [str(row["job_id"]) for row in rows]

    async def list_favorite_jobs(
        self,
        owner_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list["Job"]:
        ids = await self.list_favorite_ids(owner_id, limit=limit, offset=offset)
        jobs: list[Job] = []
        for job_id in ids:
            job = await self.get(job_id)
            if job is not None and int(job.owner_id) == int(owner_id):
                jobs.append(job)
        return jobs
