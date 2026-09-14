from __future__ import annotations

import time


_TERMINAL_SQL = "('succeeded','failed','cancelled')"


class SQLiteHistoryRepositoryMixin:
    async def hide_jobs(self, job_ids: tuple[str, ...], *, reason: str, now: float) -> int:
        if not job_ids:
            return 0
        hidden = 0
        async with self._write_transaction() as conn:
            for job_id in job_ids:
                cursor = await conn.execute(
                    """
                    INSERT OR IGNORE INTO job_visibility(job_id, hidden_at, reason, revision)
                    VALUES(?,?,?,1)
                    """,
                    (str(job_id), float(now), str(reason)),
                )
                hidden += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        return hidden

    async def is_job_hidden(self, job_id: str) -> bool:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT 1 FROM job_visibility WHERE job_id=?", (str(job_id),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def hidden_job_ids(self, job_ids: tuple[str, ...]) -> set[str]:
        if not job_ids:
            return set()
        placeholders = ",".join("?" for _ in job_ids)
        conn = self._require()
        cursor = await conn.execute(
            f"SELECT job_id FROM job_visibility WHERE job_id IN ({placeholders})",
            tuple(str(value) for value in job_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row["job_id"]) for row in rows}

    async def list_terminal_job_ids_before(
        self,
        *,
        cutoff_epoch: float,
        limit: int = 200,
        offset: int = 0,
    ) -> list[str]:
        conn = self._require()
        cursor = await conn.execute(
            f"""
            SELECT j.id AS id
            FROM jobs j
            LEFT JOIN job_visibility v ON v.job_id = j.id
            WHERE v.job_id IS NULL
              AND j.state IN {_TERMINAL_SQL}
              AND CAST(strftime('%s', j.created_at) AS INTEGER) < ?
            ORDER BY j.created_at ASC, j.rowid ASC
            LIMIT ? OFFSET ?
            """,
            (int(cutoff_epoch), max(1, int(limit)), max(0, int(offset))),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [str(row["id"]) for row in rows]

    async def job_has_active_claim(self, job_id: str, *, now: float) -> bool:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT 1 FROM job_phase_claims WHERE job_id=? AND expires_at > ? LIMIT 1",
            (str(job_id), float(now)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def job_has_unsettled_publish_step(self, job_id: str) -> bool:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT 1 FROM publish_steps s
            JOIN publish_plans p ON p.id = s.plan_id
            WHERE p.job_id=? AND s.state IN ('pending','running')
            LIMIT 1
            """,
            (str(job_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def job_has_pending_revocation(self, job_id: str) -> bool:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT 1 FROM publish_effect_revocations r
            JOIN publish_effects e ON e.id = r.effect_id
            JOIN publish_plans p ON p.id = e.plan_id
            WHERE p.job_id=? AND r.state IN ('pending','failed')
            LIMIT 1
            """,
            (str(job_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def get_maintenance_run(
        self,
        *,
        kind: str,
        business_day: str,
    ) -> dict[str, object] | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM maintenance_runs WHERE kind=? AND business_day=?",
            (str(kind), str(business_day)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def has_maintenance_targets(self, run_id: int) -> bool:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT 1 FROM maintenance_targets WHERE run_id=? LIMIT 1",
            (int(run_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def ensure_maintenance_run(
        self,
        *,
        kind: str,
        business_day: str,
        cutoff_at: float,
        now: float,
    ) -> dict[str, object]:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO maintenance_runs(
                    kind, business_day, cutoff_at, status, generation, attempt,
                    created_at, updated_at
                ) VALUES(?,?,?, 'pending', 1, 0, ?, ?)
                """,
                (str(kind), str(business_day), float(cutoff_at), float(now), float(now)),
            )
            cursor = await conn.execute(
                "SELECT * FROM maintenance_runs WHERE kind=? AND business_day=?",
                (str(kind), str(business_day)),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return dict(row) if row is not None else {}

    async def claim_maintenance_run(
        self,
        run_id: int,
        *,
        holder_id: str,
        now: float,
        lease_seconds: float = 120.0,
    ) -> dict[str, object] | None:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM maintenance_runs WHERE id=?", (int(run_id),)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            status = str(row["status"])
            if status in {"completed", "failed"}:
                return None
            lease_until = row["lease_until"]
            if (
                status == "running"
                and lease_until is not None
                and float(lease_until) > float(now)
            ):
                return None
            generation = int(row["generation"]) + (1 if status == "running" else 0)
            if status == "retry_wait" and row["next_retry_at"] is not None:
                if float(row["next_retry_at"]) > float(now):
                    return None
            await conn.execute(
                """
                UPDATE maintenance_runs
                SET status='running', holder_id=?, lease_until=?, generation=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    str(holder_id),
                    float(now) + float(lease_seconds),
                    generation,
                    float(now),
                    int(run_id),
                ),
            )
            cursor = await conn.execute(
                "SELECT * FROM maintenance_runs WHERE id=?", (int(run_id),)
            )
            updated = await cursor.fetchone()
            await cursor.close()
        return dict(updated) if updated is not None else None

    async def finish_maintenance_run(
        self,
        run_id: int,
        *,
        status: str,
        now: float,
        error: str | None = None,
        next_retry_at: float | None = None,
        attempt: int | None = None,
    ) -> None:
        finished = status in {"completed", "failed"}
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE maintenance_runs
                SET status=?, normalized_error=?, next_retry_at=?,
                    attempt=COALESCE(?, attempt),
                    finished_at=CASE WHEN ? THEN ? ELSE finished_at END,
                    lease_until=NULL, holder_id=NULL, updated_at=?
                WHERE id=?
                """,
                (
                    str(status),
                    None if error is None else str(error)[:200],
                    None if next_retry_at is None else float(next_retry_at),
                    None if attempt is None else int(attempt),
                    1 if finished else 0,
                    float(now),
                    float(now),
                    int(run_id),
                ),
            )

    async def upsert_maintenance_targets(
        self,
        run_id: int,
        targets: tuple[dict[str, object], ...],
        *,
        now: float,
    ) -> int:
        inserted = 0
        async with self._write_transaction() as conn:
            for target in targets:
                cursor = await conn.execute(
                    """
                    INSERT OR IGNORE INTO maintenance_targets(
                        run_id, job_id, display_chat_id, display_message_id,
                        phase, status, attempt, created_at, updated_at
                    ) VALUES(?,?,?,?, 'pending', 'pending', 0, ?, ?)
                    """,
                    (
                        int(run_id),
                        str(target["job_id"]),
                        target.get("chat_id"),
                        target.get("message_id"),
                        float(now),
                        float(now),
                    ),
                )
                inserted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        return inserted

    async def list_unfinished_maintenance_targets(self, run_id: int) -> list[dict[str, object]]:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT * FROM maintenance_targets
            WHERE run_id=? AND status IN ('pending','failed')
            ORDER BY id
            """,
            (int(run_id),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def set_maintenance_target_status(
        self,
        target_id: int,
        *,
        status: str,
        now: float,
        error: str | None = None,
        phase: str | None = None,
    ) -> None:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE maintenance_targets
                SET status=?, normalized_error=?, phase=COALESCE(?, phase),
                    attempt=attempt+1, updated_at=?
                WHERE id=?
                """,
                (str(status), None if error is None else str(error)[:200], phase, float(now), int(target_id)),
            )

    async def list_job_display_messages_for(
        self,
        job_ids: tuple[str, ...],
    ) -> list[dict[str, int]]:
        if not job_ids:
            return []
        placeholders = ",".join("?" for _ in job_ids)
        conn = self._require()
        cursor = await conn.execute(
            f"""
            SELECT job_id, chat_id, message_id FROM job_display_messages
            WHERE job_id IN ({placeholders})
            """,
            tuple(str(value) for value in job_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "job_id": str(row["job_id"]),
                "chat_id": int(row["chat_id"]),
                "message_id": int(row["message_id"]),
            }
            for row in rows
        ]

    async def recycle_operation_tokens(
        self,
        *,
        now: float,
        consumed_grace_seconds: float = 86400.0,
    ) -> int:
        consumed_cutoff = float(now) - float(consumed_grace_seconds)
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM operation_tokens
                WHERE expires_at < ?
                   OR (consumed_at IS NOT NULL AND consumed_at < ?)
                """,
                (float(now), consumed_cutoff),
            )
            return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    async def prune_settled_outbox(self, *, cutoff_epoch: float) -> int:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM notification_outbox
                WHERE state IN ('sent','dead') AND updated_at < ?
                """,
                (float(cutoff_epoch),),
            )
            return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    async def count_hidden_jobs(self) -> int:
        conn = self._require()
        cursor = await conn.execute("SELECT COUNT(*) AS c FROM job_visibility")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["c"]) if row is not None else 0
