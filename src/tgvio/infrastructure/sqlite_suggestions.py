from __future__ import annotations

from tgvio.domain.suggestion import SuggestionApplication


class SQLiteSuggestionRepositoryMixin:
    async def create_suggestion_application(
        self, application: SuggestionApplication
    ) -> SuggestionApplication:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO suggestion_applications(
                    id, session_id, owner_id, kind, revision_applied,
                    before_json, after_json, consumed
                ) VALUES(?,?,?,?,?,?,?,0)
                """,
                (
                    application.id,
                    application.session_id,
                    int(application.owner_id),
                    application.kind,
                    int(application.revision_applied),
                    application.before_json,
                    application.after_json,
                ),
            )
        return application

    async def get_active_suggestion_application(
        self, session_id: str
    ) -> SuggestionApplication | None:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT * FROM suggestion_applications
            WHERE session_id=? AND consumed=0
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (str(session_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._suggestion_application_from_row(row)

    async def consume_suggestion_application(self, application_id: str) -> bool:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE suggestion_applications SET consumed=1 WHERE id=? AND consumed=0",
                (str(application_id),),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _suggestion_application_from_row(row) -> SuggestionApplication:
        return SuggestionApplication(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            owner_id=int(row["owner_id"]),
            kind=str(row["kind"]),
            revision_applied=int(row["revision_applied"]),
            before_json=str(row["before_json"]),
            after_json=str(row["after_json"]),
            consumed=bool(row["consumed"]),
            created_at=row["created_at"],
        )
