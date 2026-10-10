from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CollectionMember:
    """A manual collection member that should have a copy on the storage."""

    collection_id: str
    media_id: str
    collection_name: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CollectionBackupRow:
    """What is really on the storage for one (collection, media) pair."""

    collection_id: str
    media_id: str
    relpath: str | None
    size_bytes: int | None
    attempts: int
    next_attempt_at: int


class PlayerCollectionBackupRepositoryMixin:
    """Collection folder copies on the Player storage (migration 0015)."""

    async def collection_backup_members(self) -> list[CollectionMember]:
        rows = self._require().execute(
            """
            SELECT ci.collection_id, ci.media_id, c.name, c.created_at
            FROM collection_items ci
            JOIN collections c ON c.collection_id = ci.collection_id
            JOIN media m ON m.media_id = ci.media_id
            WHERE c.kind = 'manual' AND m.active = 1 AND m.kind = 'video'
            ORDER BY ci.added_at, ci.media_id
            """
        ).fetchall()
        return [CollectionMember(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]

    async def collection_backup_collections(self) -> list[tuple[str, str, str]]:
        """Every manual collection as (id, name, created_at), oldest first."""
        rows = self._require().execute(
            "SELECT collection_id, name, created_at FROM collections WHERE kind='manual'"
            " ORDER BY created_at, collection_id"
        ).fetchall()
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    async def collection_backup_rows(self) -> list[CollectionBackupRow]:
        rows = self._require().execute(
            "SELECT collection_id, media_id, relpath, size_bytes, attempts, next_attempt_at"
            " FROM collection_backups"
        ).fetchall()
        return [
            CollectionBackupRow(
                str(r[0]), str(r[1]), None if r[2] is None else str(r[2]),
                None if r[3] is None else int(r[3]), int(r[4]), int(r[5]),
            )
            for r in rows
        ]

    async def save_collection_backup(
        self, collection_id: str, media_id: str, relpath: str, size_bytes: int
    ) -> None:
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO collection_backups(collection_id, media_id, relpath, size_bytes)
                VALUES(?,?,?,?)
                ON CONFLICT(collection_id, media_id) DO UPDATE SET
                    relpath=excluded.relpath, size_bytes=excluded.size_bytes,
                    attempts=0, next_attempt_at=0, last_error=NULL
                """,
                (collection_id, media_id, relpath, size_bytes),
            )

    async def drop_collection_backup(self, collection_id: str, media_id: str) -> None:
        async with self._write_transaction() as conn:
            conn.execute(
                "DELETE FROM collection_backups WHERE collection_id=? AND media_id=?",
                (collection_id, media_id),
            )

    async def fail_collection_backup(
        self, collection_id: str, media_id: str, *, error: str, next_attempt_at: int
    ) -> None:
        """Count a failed attempt; the row (and any copy it records) is kept."""
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO collection_backups(collection_id, media_id, attempts, next_attempt_at, last_error)
                VALUES(?,?,1,?,?)
                ON CONFLICT(collection_id, media_id) DO UPDATE SET
                    attempts=attempts+1, next_attempt_at=excluded.next_attempt_at,
                    last_error=excluded.last_error
                """,
                (collection_id, media_id, next_attempt_at, error[:64]),
            )

    async def collection_backup_status(self) -> dict[str, int]:
        row = self._require().execute(
            "SELECT COUNT(*) AS copies, COALESCE(SUM(CASE WHEN attempts>0 THEN 1 ELSE 0 END),0) AS failing"
            " FROM collection_backups"
        ).fetchone()
        return {"copies": int(row[0]), "failing": int(row[1])}
