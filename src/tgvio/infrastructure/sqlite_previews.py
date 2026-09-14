from __future__ import annotations

from tgvio.domain.preview import PreviewRequest, PreviewState


class SQLitePreviewRepositoryMixin:
    async def list_preview_request_ids(self) -> list[str]:
        cursor = await self._require().execute("SELECT id FROM preview_requests")
        rows = await cursor.fetchall()
        await cursor.close()
        # IDs become a single directory component, never an arbitrary path.
        return [str(row["id"]) for row in rows
                if str(row["id"]) and all(c.isalnum() or c in "-_" for c in str(row["id"]))]

    async def create_preview_request(self, request: PreviewRequest) -> PreviewRequest:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO preview_requests(
                    id, session_id, owner_id, revision, state,
                    cover_entry_id, cache_dir, expires_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    request.id,
                    request.session_id,
                    int(request.owner_id),
                    int(request.revision),
                    request.state.value,
                    request.cover_entry_id,
                    request.cache_dir,
                    int(request.expires_at),
                ),
            )
        created = await self.get_preview_request(request.id)
        if created is None:
            raise RuntimeError("preview request disappeared after create")
        return created

    async def get_preview_request(self, request_id: str) -> PreviewRequest | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM preview_requests WHERE id=?", (str(request_id),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._preview_from_row(row)

    async def update_preview_request(
        self,
        request_id: str,
        *,
        state: PreviewState,
        error_code: str | None = None,
        cache_dir: str | None = None,
    ) -> None:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE preview_requests
                SET state=?, error_code=COALESCE(?, error_code),
                    cache_dir=COALESCE(?, cache_dir), updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (state.value, error_code, cache_dir, str(request_id)),
            )

    async def mark_running_previews_interrupted(self) -> int:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE preview_requests
                SET state='failed', error_code='interrupted', updated_at=CURRENT_TIMESTAMP
                WHERE state IN ('pending','running')
                """
            )
            return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    @staticmethod
    def _preview_from_row(row) -> PreviewRequest:
        return PreviewRequest(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            owner_id=int(row["owner_id"]),
            chat_id=0,
            revision=int(row["revision"]),
            state=PreviewState(str(row["state"])),
            cover_entry_id=(
                None if row["cover_entry_id"] is None else int(row["cover_entry_id"])
            ),
            cache_dir=row["cache_dir"],
            error_code=row["error_code"],
            expires_at=int(row["expires_at"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
