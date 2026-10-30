from __future__ import annotations

import json

from tgvio.domain.progress import JobProgress


class SQLiteObservabilityRepositoryMixin:
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
