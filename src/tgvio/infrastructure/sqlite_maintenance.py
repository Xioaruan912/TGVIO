from __future__ import annotations

import time


class SQLiteMaintenanceRepositoryMixin:
    async def next_archive_day_seq(self, day: str) -> int:
        async with self._write_transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO archive_day_counters(day, next_seq) VALUES(?, 1)",
                (str(day),),
            )
            cursor = await conn.execute(
                "SELECT next_seq FROM archive_day_counters WHERE day=?",
                (str(day),),
            )
            row = await cursor.fetchone()
            await cursor.close()
            seq = int(row["next_seq"]) if row is not None else 1
            await conn.execute(
                "UPDATE archive_day_counters SET next_seq=? WHERE day=?",
                (seq + 1, str(day)),
            )
            return seq

    async def list_job_display_messages(self) -> list[dict[str, int]]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT chat_id, message_id FROM job_display_messages ORDER BY message_id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {"chat_id": int(row["chat_id"]), "message_id": int(row["message_id"])}
            for row in rows
        ]

    async def get_runtime_flags(self) -> dict[str, str]:
        conn = self._require()
        cursor = await conn.execute("SELECT key, value FROM runtime_flags")
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def set_runtime_flag(self, key: str, value: str) -> None:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO runtime_flags(key, value, updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(key), str(value), float(time.time())),
            )

    async def purge_terminal_history(self, *, reset_numbering: bool = True) -> dict[str, int]:
        """Delete terminal Job history and settled notifications.

        Running/held/queued Jobs are preserved. Numbering (accepted_order via
        job_schedule AUTOINCREMENT) is reset only when no Jobs remain, so the next
        day's tasks start at #1.
        """
        terminal_states = ("succeeded", "failed", "cancelled")
        placeholders = ",".join("?" for _ in terminal_states)
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                f"SELECT COUNT(*) AS c FROM jobs WHERE state IN ({placeholders})",
                terminal_states,
            )
            row = await cursor.fetchone()
            await cursor.close()
            jobs_deleted = int(row["c"]) if row is not None else 0

            await conn.execute(
                f"DELETE FROM jobs WHERE state IN ({placeholders})",
                terminal_states,
            )
            tokens = await conn.execute("DELETE FROM operation_tokens")
            tokens_deleted = tokens.rowcount if tokens.rowcount and tokens.rowcount > 0 else 0
            outbox = await conn.execute(
                "DELETE FROM notification_outbox WHERE state IN ('sent','dead')"
            )
            outbox_deleted = outbox.rowcount if outbox.rowcount and outbox.rowcount > 0 else 0
            await conn.execute("DELETE FROM collection_sessions WHERE state != 'open'")

            remaining = await conn.execute("SELECT COUNT(*) AS c FROM jobs")
            remaining_row = await remaining.fetchone()
            await remaining.close()
            remaining_jobs = int(remaining_row["c"]) if remaining_row is not None else 0
            if reset_numbering and remaining_jobs == 0:
                await conn.execute("DELETE FROM sqlite_sequence WHERE name='job_schedule'")
        return {
            "jobs_deleted": jobs_deleted,
            "tokens_deleted": tokens_deleted,
            "outbox_deleted": outbox_deleted,
            "remaining_jobs": remaining_jobs,
        }
