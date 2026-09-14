from __future__ import annotations

import json

import aiosqlite

from tgvio.domain.notifications import (
    NotificationEvent,
    OutboxEntry,
    OutboxState,
    retry_delay_seconds,
)


class SQLiteNotificationRepositoryMixin:
    async def enqueue_notification(self, event: NotificationEvent, *, now: float) -> bool:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO notification_outbox(
                    event_type, dedupe_key, payload_json, state, attempts,
                    max_attempts, next_attempt_at, created_at, updated_at
                ) VALUES(?,?,?,?,0,?,?,?,?)
                """,
                (
                    event.event_type,
                    event.dedupe_key,
                    json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                    OutboxState.PENDING.value,
                    int(event.max_attempts),
                    float(now),
                    float(now),
                    float(now),
                ),
            )
            return cursor.rowcount == 1

    async def claim_due_notifications(
        self,
        *,
        now: float,
        holder_id: str,
        limit: int = 10,
        lease_seconds: float = 120.0,
    ) -> list[OutboxEntry]:
        claimed: list[OutboxEntry] = []
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE notification_outbox
                SET state='pending', claimed_by=NULL, claim_expires_at=NULL,
                    updated_at=?
                WHERE state='claimed'
                  AND claim_expires_at IS NOT NULL
                  AND claim_expires_at < ?
                """,
                (float(now), float(now)),
            )
            cursor = await conn.execute(
                """
                SELECT * FROM notification_outbox
                WHERE state='pending' AND next_attempt_at <= ?
                ORDER BY id
                LIMIT ?
                """,
                (float(now), max(1, int(limit))),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            await conn.execute(
                f"""
                UPDATE notification_outbox
                SET state='claimed', claimed_by=?, claim_expires_at=?, updated_at=?
                WHERE id IN ({placeholders}) AND state='pending'
                """,
                (
                    holder_id,
                    float(now) + float(lease_seconds),
                    float(now),
                    *ids,
                ),
            )
            cursor = await conn.execute(
                f"SELECT * FROM notification_outbox WHERE id IN ({placeholders}) "
                "AND claimed_by=? ORDER BY id",
                (*ids, holder_id),
            )
            claimed = [self._outbox_from_row(row) for row in await cursor.fetchall()]
            await cursor.close()
        return claimed

    async def complete_notification(
        self,
        notification_id: int,
        *,
        holder_id: str,
        now: float,
    ) -> bool:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE notification_outbox
                SET state='sent', sent_at=?, updated_at=?,
                    claimed_by=NULL, claim_expires_at=NULL
                WHERE id=? AND state='claimed' AND claimed_by=?
                """,
                (float(now), float(now), int(notification_id), holder_id),
            )
            return cursor.rowcount == 1

    async def fail_notification(
        self,
        notification_id: int,
        *,
        holder_id: str,
        error_code: str,
        now: float,
        base_seconds: float = 30.0,
        cap_seconds: float = 3600.0,
        jitter: float = 0.0,
    ) -> OutboxEntry | None:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM notification_outbox WHERE id=? AND state='claimed' AND claimed_by=?",
                (int(notification_id), holder_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            attempts = int(row["attempts"]) + 1
            terminal = attempts >= int(row["max_attempts"])
            state = OutboxState.DEAD if terminal else OutboxState.PENDING
            next_attempt_at = (
                float(now)
                if terminal
                else float(now)
                + retry_delay_seconds(
                    attempts,
                    base_seconds=base_seconds,
                    cap_seconds=cap_seconds,
                    jitter=jitter,
                )
            )
            await conn.execute(
                """
                UPDATE notification_outbox
                SET state=?, attempts=?, next_attempt_at=?, last_error_code=?,
                    updated_at=?, claimed_by=NULL, claim_expires_at=NULL
                WHERE id=?
                """,
                (
                    state.value,
                    attempts,
                    next_attempt_at,
                    str(error_code)[:120],
                    float(now),
                    int(notification_id),
                ),
            )
            cursor = await conn.execute(
                "SELECT * FROM notification_outbox WHERE id=?",
                (int(notification_id),),
            )
            updated = await cursor.fetchone()
            await cursor.close()
        return self._outbox_from_row(updated) if updated is not None else None

    async def count_notification_outbox(self) -> dict[str, int]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT state, COUNT(*) AS count FROM notification_outbox GROUP BY state"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        counts = {state.value: 0 for state in OutboxState}
        for row in rows:
            counts[str(row["state"])] = int(row["count"])
        return counts

    async def get_notification_candidates(
        self,
        *,
        since_epoch: int,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """Bounded terminal-event feed for idempotent outbox reconciliation."""

        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT j.id AS job_id,
                   j.state AS state,
                   j.error_code AS error_code,
                   CAST(strftime('%s', j.updated_at) AS INTEGER) AS occurred_at,
                   (SELECT COUNT(*) FROM job_items i WHERE i.job_id=j.id) AS media_count,
                   (SELECT COALESCE(SUM(i.size_bytes),0) FROM job_items i WHERE i.job_id=j.id) AS bytes,
                   (SELECT s.accepted_order FROM job_schedule s WHERE s.job_id=j.id) AS accepted_order,
                   json_extract(j.policy_json, '$.auto_recovery_job.status') AS recovery_status
            FROM jobs j
            WHERE j.state IN ('succeeded','failed','cancelled')
              AND CAST(strftime('%s', j.updated_at) AS INTEGER) >= ?
            ORDER BY j.updated_at DESC
            LIMIT ?
            """,
            (int(since_epoch), max(1, int(limit))),
        )
        jobs = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        cursor = await conn.execute(
            """
            SELECT a.id AS package_id,
                   a.job_id AS job_id,
                   a.state AS state,
                   CAST(strftime('%s', a.updated_at) AS INTEGER) AS occurred_at,
                   (SELECT COUNT(*) FROM archive_objects o WHERE o.package_id=a.id) AS media_count,
                   (SELECT COALESCE(SUM(o.size_bytes),0) FROM archive_objects o WHERE o.package_id=a.id) AS bytes,
                   json_extract(j.policy_json, '$.auto_recovery_archive.status') AS recovery_status
            FROM archive_packages a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.state IN ('committed','failed','cancelled')
              AND CAST(strftime('%s', a.updated_at) AS INTEGER) >= ?
            ORDER BY a.updated_at DESC
            LIMIT ?
            """,
            (int(since_epoch), max(1, int(limit))),
        )
        packages = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        return [{"kind": "job", **row} for row in jobs] + [
            {"kind": "archive", **row} for row in packages
        ]

    @staticmethod
    def _outbox_from_row(row: aiosqlite.Row) -> OutboxEntry:
        payload = json.loads(row["payload_json"] or "{}")
        if not isinstance(payload, dict):
            payload = {}
        return OutboxEntry(
            id=int(row["id"]),
            event_type=str(row["event_type"]),
            dedupe_key=str(row["dedupe_key"]),
            payload=payload,
            state=OutboxState(str(row["state"])),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            claimed_by=(None if row["claimed_by"] is None else str(row["claimed_by"])),
            claim_expires_at=(
                None if row["claim_expires_at"] is None else float(row["claim_expires_at"])
            ),
            last_error_code=(
                None if row["last_error_code"] is None else str(row["last_error_code"])
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            sent_at=(None if row["sent_at"] is None else float(row["sent_at"])),
        )
