from __future__ import annotations

import time

from tgvio_player.domain.watched import parse_forget_after_days
from tgvio_player.infrastructure.video_query import WATCH_CUTOFF_SQL


class PlayerWatchedRepositoryMixin:
    async def mark_watched(self, media_id: str, *, at: int | None = None) -> bool:
        """Record a watch; False when the media is not in the catalog."""
        async with self._write_transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO player_watched(media_id, watched_at)
                SELECT media_id, ? FROM media WHERE media_id=?
                ON CONFLICT(media_id) DO UPDATE SET watched_at=excluded.watched_at
                """,
                (int(time.time() if at is None else at), media_id),
            )
            return cursor.rowcount > 0

    async def watched_media_ids(self) -> set[str]:
        """Media whose mark still counts under the forget window."""
        rows = self._require().execute(
            f"SELECT media_id FROM player_watched WHERE watched_at >= {WATCH_CUTOFF_SQL}"
        ).fetchall()
        return {str(row["media_id"]) for row in rows}

    async def get_forget_after_days(self) -> int | None:
        row = self._require().execute(
            "SELECT forget_after_days FROM player_watch_settings WHERE singleton=1"
        ).fetchone()
        return None if row is None else row["forget_after_days"]

    async def set_forget_after_days(self, days: int | None) -> None:
        days = parse_forget_after_days(days)
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO player_watch_settings(singleton, forget_after_days, updated_at)
                VALUES(1,?,?)
                ON CONFLICT(singleton) DO UPDATE SET
                    forget_after_days=excluded.forget_after_days, updated_at=excluded.updated_at
                """,
                (days, int(time.time())),
            )
