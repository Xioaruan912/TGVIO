from __future__ import annotations

import json

from tgvio.domain.progress import JobProgress


class SQLiteObservabilityRepositoryMixin:
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

    async def get_diagnostic_aggregates(self) -> dict[str, object]:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT
                COALESCE((SELECT paused FROM queue_controls WHERE singleton=1), 0)
                    AS queue_paused,
                COALESCE(SUM(CASE
                    WHEN j.state NOT IN ('succeeded','failed','cancelled')
                     AND COALESCE(c.hold_requested,0)=0 THEN 1 ELSE 0 END), 0)
                    AS scheduler_active,
                COALESCE(SUM(CASE
                    WHEN j.state NOT IN ('succeeded','failed','cancelled')
                     AND COALESCE(c.hold_requested,0)=1 THEN 1 ELSE 0 END), 0)
                    AS scheduler_held,
                COALESCE(SUM(CASE
                    WHEN j.state IN ('planned','publishing')
                     AND COALESCE(c.hold_requested,0)=0 THEN 1 ELSE 0 END), 0)
                    AS scheduler_ready,
                COALESCE(SUM(CASE
                    WHEN j.state='failed' AND (
                        COALESCE(j.error_code,'') IN ('publish_partial','publish_uncertain')
                        OR COALESCE(
                            json_extract(j.policy_json, '$.auto_recovery_job.status'),
                            ''
                        )='quarantined'
                    ) THEN 1 ELSE 0 END), 0)
                    AS scheduler_blocked
            FROM jobs j
            LEFT JOIN job_controls c ON c.job_id=j.id
            """
        )
        scheduler_row = await cursor.fetchone()
        await cursor.close()

        cursor = await conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN a.state='planned' THEN 1 ELSE 0 END), 0)
                    AS archive_planned,
                COALESCE(SUM(CASE
                    WHEN a.state IN ('staging','uploading','verifying') THEN 1 ELSE 0 END), 0)
                    AS archive_transferring,
                COALESCE(SUM(CASE WHEN a.state='committed' THEN 1 ELSE 0 END), 0)
                    AS archive_committed,
                COALESCE(SUM(CASE WHEN a.state='failed' THEN 1 ELSE 0 END), 0)
                    AS archive_failed,
                COALESCE(SUM(CASE
                    WHEN a.state='failed'
                     AND COALESCE(
                        json_extract(j.policy_json, '$.auto_recovery_archive.status'),
                        ''
                     )='scheduled'
                    THEN 1 ELSE 0 END), 0)
                    AS archive_retry_wait
            FROM archive_packages a
            JOIN jobs j ON j.id=a.job_id
            """
        )
        archive_row = await cursor.fetchone()
        await cursor.close()

        cursor = await conn.execute(
            """
            SELECT
                CAST(strftime('%s','now') AS INTEGER) AS database_now_epoch,
                COUNT(CASE WHEN expires_at > CAST(strftime('%s','now') AS INTEGER)
                           THEN 1 END) AS runtime_lease_active_count,
                MAX(generation) AS runtime_lease_generation,
                MAX(expires_at) AS runtime_lease_expires_at
            FROM runtime_leases
            WHERE lease_name='telegram-runtime'
            """
        )
        lease_row = await cursor.fetchone()
        await cursor.close()

        result: dict[str, object] = {}
        if scheduler_row is not None:
            for key in (
                "queue_paused",
                "scheduler_active",
                "scheduler_held",
                "scheduler_ready",
                "scheduler_blocked",
            ):
                result[key] = int(scheduler_row[key] or 0)
        if archive_row is not None:
            for key in (
                "archive_planned",
                "archive_transferring",
                "archive_committed",
                "archive_failed",
                "archive_retry_wait",
            ):
                result[key] = int(archive_row[key] or 0)
        if lease_row is not None:
            result["database_now_epoch"] = int(lease_row["database_now_epoch"] or 0)
            result["runtime_lease_active_count"] = int(
                lease_row["runtime_lease_active_count"] or 0
            )
            result["runtime_lease_generation"] = (
                None
                if lease_row["runtime_lease_generation"] is None
                else int(lease_row["runtime_lease_generation"])
            )
            result["runtime_lease_expires_at"] = (
                None
                if lease_row["runtime_lease_expires_at"] is None
                else int(lease_row["runtime_lease_expires_at"])
            )
        return result

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
