from __future__ import annotations

import sqlite3
import time

from tgvio_player.domain.tags import MAX_TAGS, InvalidTag, Tag, clean_tag_name


class PlayerTagRepositoryMixin:
    """Tags and which videos carry them (migration 0018)."""

    async def list_tags(self) -> list[Tag]:
        rows = self._require().execute(
            """
            SELECT t.tag_id, t.name, COUNT(m.media_id) AS count
            FROM player_tags t
            LEFT JOIN player_media_tags mt ON mt.tag_id=t.tag_id
            LEFT JOIN media m ON m.media_id=mt.media_id AND m.active=1
            GROUP BY t.tag_id ORDER BY t.name COLLATE NOCASE
            """
        ).fetchall()
        return [Tag(int(row[0]), str(row[1]), int(row[2])) for row in rows]

    async def create_tag(self, name: str) -> Tag:
        """The tag of that name, made if it does not exist yet."""
        name = clean_tag_name(name)
        async with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT tag_id, name FROM player_tags WHERE name=?", (name,)
            ).fetchone()
            if row is not None:
                return Tag(int(row[0]), str(row[1]))
            if conn.execute("SELECT COUNT(*) FROM player_tags").fetchone()[0] >= MAX_TAGS:
                raise InvalidTag(f"at most {MAX_TAGS} tags")
            cursor = conn.execute(
                "INSERT INTO player_tags(name, created_at) VALUES(?,?)", (name, int(time.time()))
            )
            return Tag(int(cursor.lastrowid), name)

    async def rename_tag(self, tag_id: int, name: str) -> bool:
        name = clean_tag_name(name)
        async with self._write_transaction() as conn:
            try:
                cursor = conn.execute(
                    "UPDATE player_tags SET name=? WHERE tag_id=?", (name, int(tag_id))
                )
            except sqlite3.IntegrityError:
                raise InvalidTag("another tag has that name") from None
            return cursor.rowcount > 0

    async def delete_tag(self, tag_id: int) -> bool:
        async with self._write_transaction() as conn:
            return conn.execute(
                "DELETE FROM player_tags WHERE tag_id=?", (int(tag_id),)
            ).rowcount > 0

    async def media_tag_ids(self, media_id: str) -> list[int]:
        rows = self._require().execute(
            "SELECT tag_id FROM player_media_tags WHERE media_id=? ORDER BY tag_id", (media_id,)
        ).fetchall()
        return [int(row[0]) for row in rows]

    async def set_media_tag(self, media_id: str, tag_id: int, *, enabled: bool) -> bool:
        """False when the media or the tag does not exist."""
        async with self._write_transaction() as conn:
            if not enabled:
                conn.execute(
                    "DELETE FROM player_media_tags WHERE media_id=? AND tag_id=?",
                    (media_id, int(tag_id)),
                )
                return True
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO player_media_tags(media_id, tag_id, tagged_at)
                SELECT m.media_id, t.tag_id, ? FROM media m, player_tags t
                WHERE m.media_id=? AND t.tag_id=?
                """,
                (int(time.time()), media_id, int(tag_id)),
            )
            if cursor.rowcount:
                return True
            return conn.execute(
                "SELECT 1 FROM player_media_tags WHERE media_id=? AND tag_id=?",
                (media_id, int(tag_id)),
            ).fetchone() is not None
