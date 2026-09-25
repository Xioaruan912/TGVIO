from __future__ import annotations

import time

from tgvio_player.application.ports import FavoriteSyncJob
from tgvio_player.domain.storage_settings import PlayerStorageSettings


class PlayerFavoriteRepositoryMixin:
    async def get_storage_settings(self) -> PlayerStorageSettings:
        row = self._require().execute(
            """
            SELECT endpoint_url, player_root, favorites_dir,
                   username_ciphertext, password_ciphertext, revision
            FROM player_storage_settings WHERE singleton=1
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("Player storage settings are not initialized")
        return PlayerStorageSettings(
            endpoint_url=str(row["endpoint_url"]),
            player_root=str(row["player_root"]),
            favorites_dir=str(row["favorites_dir"]),
            username_ciphertext=bytes(row["username_ciphertext"])
            if row["username_ciphertext"] is not None else None,
            password_ciphertext=bytes(row["password_ciphertext"])
            if row["password_ciphertext"] is not None else None,
            revision=int(row["revision"]),
        )

    async def save_storage_settings(self, settings: PlayerStorageSettings) -> None:
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO player_storage_settings(
                    singleton, endpoint_url, player_root, favorites_dir,
                    username_ciphertext, password_ciphertext, revision
                ) VALUES(1,?,?,?,?,?,?)
                ON CONFLICT(singleton) DO UPDATE SET
                    endpoint_url=excluded.endpoint_url,
                    player_root=excluded.player_root,
                    favorites_dir=excluded.favorites_dir,
                    username_ciphertext=excluded.username_ciphertext,
                    password_ciphertext=excluded.password_ciphertext,
                    revision=excluded.revision
                """,
                (settings.endpoint_url, settings.player_root, settings.favorites_dir,
                 settings.username_ciphertext, settings.password_ciphertext, settings.revision),
            )

    async def set_global_favorite(self, media_id: str, enabled: bool) -> None:
        async with self._write_transaction() as conn:
            if enabled:
                conn.execute(
                    "INSERT INTO player_global_favorites(media_id, created_at) VALUES(?,?) "
                    "ON CONFLICT(media_id) DO NOTHING",
                    (media_id, int(time.time())),
                )
            else:
                conn.execute("DELETE FROM player_global_favorites WHERE media_id=?", (media_id,))

    async def is_global_favorite(self, media_id: str) -> bool:
        row = self._require().execute(
            "SELECT 1 FROM player_global_favorites WHERE media_id=?", (media_id,)
        ).fetchone()
        return row is not None

    async def list_global_favorite_page(
        self, *, limit: int, before: tuple[int, str] | None
    ) -> list[tuple[str, int]]:
        clauses = ["media.active=1", "media.kind='video'"]
        params: list[object] = []
        if before is not None:
            created_at, media_id = before
            clauses.append(
                "(favorite.created_at < ? OR (favorite.created_at = ? AND favorite.media_id > ?))"
            )
            params.extend((int(created_at), int(created_at), media_id))
        params.append(max(1, int(limit)))
        rows = self._require().execute(
            """
            SELECT favorite.media_id, favorite.created_at
            FROM player_global_favorites favorite
            JOIN media ON media.media_id=favorite.media_id
            WHERE """ + " AND ".join(clauses) +
            " ORDER BY favorite.created_at DESC, favorite.media_id ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [(str(row["media_id"]), int(row["created_at"])) for row in rows]

    async def enqueue_favorite_sync(self, media_id: str, operation: str) -> None:
        if operation not in {"upload", "delete"}:
            raise ValueError("invalid favorite sync operation")
        now = int(time.time())
        async with self._write_transaction() as conn:
            existing = conn.execute(
                """
                SELECT job_id FROM favorite_sync
                WHERE media_id=? AND operation=? AND status IN ('pending','running','retry')
                LIMIT 1
                """,
                (media_id, operation),
            ).fetchone()
            if existing is not None:
                return
            conn.execute(
                """
                INSERT INTO favorite_sync(
                    media_id, operation, status, created_at, updated_at
                ) VALUES(?,?,'pending',?,?)
                """,
                (media_id, operation, now, now),
            )

    async def claim_favorite_sync(self, *, limit: int) -> list[FavoriteSyncJob]:
        now = int(time.time())
        async with self._write_transaction() as conn:
            rows = conn.execute(
                """
                SELECT job_id, media_id, operation, attempts, status, error_code, intent_persisted
                FROM favorite_sync
                WHERE status IN ('pending','retry') AND next_attempt_at<=?
                  AND (operation='upload' OR intent_persisted=1)
                ORDER BY job_id LIMIT ?
                """,
                (now, max(0, int(limit))),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE favorite_sync
                    SET status='running', attempts=attempts+1, claimed_at=?, updated_at=?, error_code=NULL
                    WHERE job_id=? AND status IN ('pending','retry')
                    """,
                    (now, now, int(row["job_id"])),
                )
            return [
                FavoriteSyncJob(
                    job_id=int(row["job_id"]), media_id=str(row["media_id"]),
                    operation=str(row["operation"]), status="running",
                    attempts=int(row["attempts"]) + 1, error_code=None,
                    intent_persisted=bool(row["intent_persisted"]),
                )
                for row in rows
            ]

    async def list_pending_favorite_sync(self) -> list[FavoriteSyncJob]:
        rows = self._require().execute(
            """
            SELECT job_id, media_id, operation, attempts, status, error_code, intent_persisted
            FROM favorite_sync WHERE status IN ('pending','running','retry')
            ORDER BY job_id
            """
        ).fetchall()
        return [
            FavoriteSyncJob(
                job_id=int(row["job_id"]), media_id=str(row["media_id"]),
                operation=str(row["operation"]), status=str(row["status"]),
                attempts=int(row["attempts"]), error_code=row["error_code"],
                intent_persisted=bool(row["intent_persisted"]),
            )
            for row in rows
        ]

    async def finish_favorite_sync(
        self, job_id: int, status: str, error_code: str | None, *, retry_at: int | None = None
    ) -> None:
        if status not in {"retry", "synced", "failed"}:
            raise ValueError("invalid terminal favorite sync status")
        if status == "synced" and error_code is not None:
            raise ValueError("synced favorite jobs cannot have an error")
        now = int(time.time())
        async with self._write_transaction() as conn:
            conn.execute(
                """
                UPDATE favorite_sync
                SET status=?, error_code=?, next_attempt_at=?, claimed_at=NULL, updated_at=?
                WHERE job_id=?
                """,
                (status, error_code, int(retry_at) if retry_at is not None else now, now, job_id),
            )

    async def save_favorite_location(
        self, media_id: str, relpath: str, size_bytes: int, mime_type: str
    ) -> None:
        if size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO favorite_locations(media_id, relpath, size_bytes, mime_type, verified_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET
                    relpath=excluded.relpath, size_bytes=excluded.size_bytes,
                    mime_type=excluded.mime_type, verified_at=excluded.verified_at
                """,
                (media_id, relpath, size_bytes, mime_type, int(time.time())),
            )

    async def get_favorite_location(self, media_id: str) -> tuple[str, int, str] | None:
        row = self._require().execute(
            "SELECT relpath, size_bytes, mime_type FROM favorite_locations WHERE media_id=?",
            (media_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["relpath"]), int(row["size_bytes"]), str(row["mime_type"])

    async def delete_favorite_location(self, media_id: str) -> None:
        async with self._write_transaction() as conn:
            conn.execute("DELETE FROM favorite_locations WHERE media_id=?", (media_id,))

    async def favorite_sync_summary(self) -> dict[str, int | None]:
        row = self._require().execute(
            """
            SELECT
                SUM(CASE WHEN status IN ('pending','running','retry') THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                MAX(CASE WHEN status='synced' THEN updated_at END) AS last_success_at
            FROM favorite_sync
            """
        ).fetchone()
        return {
            "pending": int(row["pending"] or 0),
            "failed": int(row["failed"] or 0),
            "last_success_at": int(row["last_success_at"])
            if row["last_success_at"] is not None else None,
        }

    async def recover_interrupted_favorite_sync(self) -> None:
        now = int(time.time())
        async with self._write_transaction() as conn:
            conn.execute(
                """
                UPDATE favorite_sync
                SET status='retry', claimed_at=NULL, next_attempt_at=?, updated_at=?,
                    error_code=COALESCE(error_code, 'process_interrupted')
                WHERE status='running'
                """,
                (now, now),
            )

    async def retry_failed_favorite_sync(self) -> int:
        now = int(time.time())
        async with self._write_transaction() as conn:
            cursor = conn.execute(
                """UPDATE favorite_sync
                   SET status='retry', error_code=NULL, next_attempt_at=?,
                       claimed_at=NULL, updated_at=?
                   WHERE status='failed'""",
                (now, now),
            )
            return int(cursor.rowcount)

    async def mark_favorite_delete_intent(self, media_id: str) -> None:
        async with self._write_transaction() as conn:
            conn.execute(
                """
                UPDATE favorite_sync SET intent_persisted=1, updated_at=?
                WHERE media_id=? AND operation='delete'
                  AND status IN ('pending','running','retry')
                """,
                (int(time.time()), media_id),
            )

    async def restore_favorite_copy(
        self, media_id: str, relpath: str, size_bytes: int, mime_type: str, created_at: int
    ) -> None:
        if size_bytes < 0 or not mime_type.startswith("video/"):
            raise ValueError("invalid restored favorite metadata")
        now = int(time.time())
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO media(media_id, kind, mime_type, size_bytes, active, first_seen_at, last_seen_at)
                VALUES(?, 'video', ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(media_id) DO UPDATE SET
                    kind='video', mime_type=excluded.mime_type, size_bytes=excluded.size_bytes,
                    active=1, last_seen_at=CURRENT_TIMESTAMP
                """,
                (media_id, mime_type, size_bytes),
            )
            conn.execute(
                """
                INSERT INTO player_global_favorites(media_id, created_at) VALUES(?,?)
                ON CONFLICT(media_id) DO UPDATE SET created_at=MIN(created_at, excluded.created_at)
                """,
                (media_id, created_at),
            )
            conn.execute(
                """
                INSERT INTO favorite_locations(media_id, relpath, size_bytes, mime_type, verified_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET
                    relpath=excluded.relpath, size_bytes=excluded.size_bytes,
                    mime_type=excluded.mime_type, verified_at=excluded.verified_at
                """,
                (media_id, relpath, size_bytes, mime_type, now),
            )

    async def restore_pending_favorite(self, media_id: str, created_at: int) -> None:
        now = int(time.time())
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO media(media_id, kind, size_bytes, active, first_seen_at, last_seen_at)
                VALUES(?, 'video', 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(media_id) DO NOTHING
                """,
                (media_id,),
            )
            conn.execute(
                """
                INSERT INTO player_global_favorites(media_id, created_at) VALUES(?,?)
                ON CONFLICT(media_id) DO UPDATE SET created_at=MIN(created_at, excluded.created_at)
                """,
                (media_id, created_at),
            )
            conn.execute(
                """
                INSERT INTO favorite_sync(media_id, operation, status, created_at, updated_at)
                SELECT ?, 'upload', 'pending', ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM favorite_sync WHERE media_id=? AND operation='upload'
                      AND status IN ('pending','running','retry')
                )
                """,
                (media_id, now, now, media_id),
            )

    async def list_favorite_locations(self) -> list[tuple[str, str, int, str]]:
        rows = self._require().execute(
            "SELECT media_id, relpath, size_bytes, mime_type FROM favorite_locations ORDER BY media_id"
        ).fetchall()
        return [
            (str(row["media_id"]), str(row["relpath"]), int(row["size_bytes"]), str(row["mime_type"]))
            for row in rows
        ]
