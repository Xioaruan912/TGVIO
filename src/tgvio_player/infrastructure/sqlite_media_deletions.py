from __future__ import annotations

# Whether a media item is visible. A media item that is queued for deletion stays
# hidden however its archive locations look, so a catalog refresh can never bring
# it back while its files are still being removed. Shared by the catalog refresh
# and by an undo, so both decide visibility the same way.
MEDIA_ACTIVE_CASE = """
    CASE
        WHEN EXISTS (
            SELECT 1 FROM player_media_deletions d WHERE d.media_id = media.media_id
        ) THEN 0
        WHEN EXISTS (
            SELECT 1
            FROM media_locations ml
            JOIN catalog_packages cp ON cp.package_id = ml.package_id
            WHERE ml.media_id = media.media_id
              AND ml.active = 1
              AND cp.active = 1
        ) OR EXISTS (
            SELECT 1 FROM favorite_locations fl
            WHERE fl.media_id = media.media_id
        ) THEN 1 ELSE 0 END
"""


class PlayerMediaDeletionRepositoryMixin:
    """The durable queue of permanent deletions (migration 0014)."""

    async def request_media_deletion(self, media_id: str, *, now: int, undo_until: int) -> int:
        """Queue a deletion and hide the media in one transaction.

        Asking again for a media already queued keeps the first request (and its undo
        window). Returns the undo deadline that applies.
        """
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO player_media_deletions(
                    media_id, requested_at, undo_until, next_attempt_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(media_id) DO NOTHING
                """,
                (media_id, now, undo_until, undo_until),
            )
            conn.execute("UPDATE media SET active=0 WHERE media_id=?", (media_id,))
            row = conn.execute(
                "SELECT undo_until FROM player_media_deletions WHERE media_id=?", (media_id,)
            ).fetchone()
            return int(row["undo_until"])

    async def cancel_media_deletion(self, media_id: str, *, now: int) -> bool:
        """Undo a queued deletion that has not started; the media becomes visible again."""
        async with self._write_transaction() as conn:
            removed = conn.execute(
                """
                DELETE FROM player_media_deletions
                WHERE media_id=? AND started=0 AND undo_until>?
                """,
                (media_id, now),
            ).rowcount
            if not removed:
                return False
            conn.execute(
                f"UPDATE media SET active = {MEDIA_ACTIVE_CASE} WHERE media_id=?", (media_id,)
            )
            return True

    async def is_media_deletion_queued(self, media_id: str) -> bool:
        row = self._require().execute(
            "SELECT 1 FROM player_media_deletions WHERE media_id=?", (media_id,)
        ).fetchone()
        return row is not None

    async def claim_due_media_deletion(self, *, now: int) -> tuple[str, int] | None:
        """Take the deletion that is due soonest; from here on it cannot be undone."""
        async with self._write_transaction() as conn:
            row = conn.execute(
                """
                SELECT media_id, attempts FROM player_media_deletions
                WHERE next_attempt_at<=? AND undo_until<=?
                ORDER BY next_attempt_at, requested_at
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE player_media_deletions SET started=1 WHERE media_id=?",
                (row["media_id"],),
            )
            return str(row["media_id"]), int(row["attempts"])

    async def next_media_deletion_due(self) -> int | None:
        row = self._require().execute(
            "SELECT MIN(MAX(next_attempt_at, undo_until)) AS due FROM player_media_deletions"
        ).fetchone()
        return None if row is None or row["due"] is None else int(row["due"])

    async def retry_media_deletion(self, media_id: str, *, error: str, next_attempt_at: int) -> int:
        async with self._write_transaction() as conn:
            conn.execute(
                """
                UPDATE player_media_deletions
                SET attempts=attempts+1, next_attempt_at=?, last_error=?
                WHERE media_id=?
                """,
                (next_attempt_at, error[:64], media_id),
            )
            row = conn.execute(
                "SELECT attempts FROM player_media_deletions WHERE media_id=?", (media_id,)
            ).fetchone()
            return 0 if row is None else int(row["attempts"])

    async def complete_media_deletion(self, media_id: str) -> None:
        async with self._write_transaction() as conn:
            conn.execute("DELETE FROM player_media_deletions WHERE media_id=?", (media_id,))

    async def retry_media_deletions_now(self, *, now: int) -> int:
        """Bring every waiting retry forward (the viewer asked to try again)."""
        async with self._write_transaction() as conn:
            return conn.execute(
                """
                UPDATE player_media_deletions SET next_attempt_at=?
                WHERE attempts>0 AND next_attempt_at>?
                """,
                (now, now),
            ).rowcount

    async def media_deletion_status(self) -> dict[str, int]:
        row = self._require().execute(
            """
            SELECT COUNT(*) AS pending,
                   COALESCE(SUM(CASE WHEN attempts>0 THEN 1 ELSE 0 END), 0) AS retrying
            FROM player_media_deletions
            """
        ).fetchone()
        return {"pending": int(row["pending"]), "retrying": int(row["retrying"])}

    async def deletion_location_records(self, media_id: str) -> list[tuple[str, str, str]]:
        """Archive copies still to remove; unlike playback reads, a hidden media counts."""
        rows = self._require().execute(
            """
            SELECT ml.package_id, cp.remote_path, ml.remote_relpath
            FROM media_locations ml
            JOIN catalog_packages cp ON cp.package_id=ml.package_id
            WHERE ml.media_id=? AND ml.active=1 AND cp.active=1
            ORDER BY cp.package_id, ml.remote_relpath
            """,
            (media_id,),
        ).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]
