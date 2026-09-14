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
