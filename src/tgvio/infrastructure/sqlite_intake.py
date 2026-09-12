from __future__ import annotations

import json

import aiosqlite

from tgvio.domain.intake import (
    CollectionEntry,
    CollectionEntryKind,
    CollectionSession,
    CollectionState,
    IntakeEventKey,
    JobDisplayMessage,
    SpoilerMode,
    UserPreference,
)
from tgvio.domain.job import Job


class SQLiteIntakeRepositoryMixin:
    async def lookup_intake_events(
        self,
        keys: tuple[IntakeEventKey, ...],
    ) -> dict[IntakeEventKey, str]:
        if not keys:
            return {}
        conn = self._require()
        found: dict[IntakeEventKey, str] = {}
        for start in range(0, len(keys), 400):
            chunk = keys[start : start + 400]
            placeholders = ",".join("(?,?)" for _ in chunk)
            params: list[int] = []
            for key in chunk:
                params.extend((key.source_chat_id, key.source_message_id))
            cursor = await conn.execute(
                f"""
                SELECT source_chat_id, source_message_id, job_id
                FROM intake_events
                WHERE (source_chat_id, source_message_id) IN ({placeholders})
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                found[
                    IntakeEventKey(
                        int(row["source_chat_id"]),
                        int(row["source_message_id"]),
                    )
                ] = str(row["job_id"])
        return found

    async def create_with_intake_events(
        self,
        job: Job,
        events: tuple[tuple[IntakeEventKey, int], ...],
    ) -> bool:
        async with self._write_transaction() as conn:
            if events:
                for start in range(0, len(events), 400):
                    chunk = events[start : start + 400]
                    placeholders = ",".join("(?,?)" for _ in chunk)
                    params: list[int] = []
                    for key, _item_index in chunk:
                        params.extend((key.source_chat_id, key.source_message_id))
                    cursor = await conn.execute(
                        f"""
                        SELECT 1
                        FROM intake_events
                        WHERE (source_chat_id, source_message_id) IN ({placeholders})
                        LIMIT 1
                        """,
                        tuple(params),
                    )
                    duplicate = await cursor.fetchone()
                    await cursor.close()
                    if duplicate is not None:
                        return False
            await self._insert_new_job(conn, job)
            for key, item_index in events:
                await conn.execute(
                    """
                    INSERT INTO intake_events(
                        source_chat_id, source_message_id, owner_id, job_id, item_index
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        key.source_chat_id,
                        key.source_message_id,
                        job.owner_id,
                        job.id,
                        item_index,
                    ),
                )
        return True

    async def get_open_collection(
        self,
        owner_id: int,
        chat_id: int,
    ) -> CollectionSession | None:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT *
            FROM collection_sessions
            WHERE owner_id=? AND chat_id=? AND state='open'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (owner_id, chat_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._collection_session_from_row(row)

    async def get_collection(self, session_id: str) -> CollectionSession | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM collection_sessions WHERE id=?",
            (session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._collection_session_from_row(row)

    async def create_collection(self, session: CollectionSession) -> CollectionSession:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT *
                FROM collection_sessions
                WHERE owner_id=? AND chat_id=? AND state='open'
                LIMIT 1
                """,
                (session.owner_id, session.chat_id),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is not None:
                return self._collection_session_from_row(existing)
            await conn.execute(
                """
                INSERT INTO collection_sessions(
                    id, owner_id, chat_id, state, status_chat_id, status_message_id,
                    finalized_job_ids_json
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    session.id,
                    session.owner_id,
                    session.chat_id,
                    session.state.value,
                    session.status_chat_id,
                    session.status_message_id,
                    json.dumps(list(session.finalized_job_ids), separators=(",", ":")),
                ),
            )
        created = await self.get_collection(session.id)
        if created is None:
            raise RuntimeError("collection session disappeared after create")
        return created

    async def set_collection_status_message(
        self,
        session_id: str,
        chat_id: int,
        message_id: int,
    ) -> CollectionSession:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE collection_sessions
                SET status_chat_id=?, status_message_id=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (chat_id, message_id, session_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"collection not found: {session_id}")
        updated = await self.get_collection(session_id)
        if updated is None:
            raise RuntimeError("collection session disappeared after status update")
        return updated

    async def append_collection_entries(
        self,
        session_id: str,
        entries: tuple[CollectionEntry, ...],
    ) -> list[CollectionEntry]:
        if not entries:
            return []
        inserted_ids: list[int] = []
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT state FROM collection_sessions WHERE id=?",
                (session_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(f"collection not found: {session_id}")
            if CollectionState(row["state"]) != CollectionState.OPEN:
                raise ValueError("collection is not open")
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) AS max_ordinal FROM collection_entries WHERE session_id=?",
                (session_id,),
            )
            ordinal_row = await cursor.fetchone()
            await cursor.close()
            next_ordinal = int(ordinal_row["max_ordinal"]) + 1
            for entry in entries:
                if entry.source_chat_id is not None and entry.source_message_id is not None:
                    cursor = await conn.execute(
                        """
                        SELECT id FROM collection_entries
                        WHERE session_id=? AND source_chat_id=? AND source_message_id=?
                        LIMIT 1
                        """,
                        (session_id, entry.source_chat_id, entry.source_message_id),
                    )
                    duplicate = await cursor.fetchone()
                    await cursor.close()
                    if duplicate is not None:
                        continue
                cursor = await conn.execute(
                    """
                    INSERT INTO collection_entries(
                        session_id, ordinal, entry_kind, source_chat_id,
                        source_message_id, payload_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        session_id,
                        next_ordinal,
                        entry.kind.value,
                        entry.source_chat_id,
                        entry.source_message_id,
                        json.dumps(entry.payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                inserted_ids.append(int(cursor.lastrowid))
                next_ordinal += 1
            await conn.execute(
                "UPDATE collection_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (session_id,),
            )
        if not inserted_ids:
            return []
        conn = self._require()
        placeholders = ",".join("?" for _ in inserted_ids)
        cursor = await conn.execute(
            f"SELECT * FROM collection_entries WHERE id IN ({placeholders}) ORDER BY ordinal",
            tuple(inserted_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._collection_entry_from_row(row) for row in rows]

    async def list_collection_entries(self, session_id: str) -> list[CollectionEntry]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM collection_entries WHERE session_id=? ORDER BY ordinal",
            (session_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._collection_entry_from_row(row) for row in rows]

    async def finalize_collection(
        self,
        session_id: str,
        job_ids: tuple[str, ...],
    ) -> CollectionSession:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE collection_sessions
                SET state='finalized', finalized_job_ids_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND state='open'
                """,
                (json.dumps(list(job_ids), separators=(",", ":")), session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("collection is not open")
        finalized = await self.get_collection(session_id)
        if finalized is None:
            raise RuntimeError("collection session disappeared after finalize")
        return finalized

    async def cancel_collection(self, session_id: str) -> CollectionSession:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE collection_sessions
                SET state='cancelled', updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND state='open'
                """,
                (session_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("collection is not open")
        cancelled = await self.get_collection(session_id)
        if cancelled is None:
            raise RuntimeError("collection session disappeared after cancel")
        return cancelled

    async def get_user_preference(self, owner_id: int) -> UserPreference:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM user_preferences WHERE owner_id=?",
            (owner_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return UserPreference(owner_id=owner_id)
        return UserPreference(
            owner_id=int(row["owner_id"]),
            spoiler_mode=SpoilerMode(row["spoiler_mode"]),
            updated_at=row["updated_at"],
        )

    async def set_user_spoiler_mode(self, owner_id: int, mode: SpoilerMode) -> UserPreference:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO user_preferences(owner_id, spoiler_mode)
                VALUES(?,?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    spoiler_mode=excluded.spoiler_mode,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (owner_id, mode.value),
            )
        return await self.get_user_preference(owner_id)

    async def get_job_display_message(self, job_id: str) -> JobDisplayMessage | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM job_display_messages WHERE job_id=?",
            (job_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return JobDisplayMessage(
            job_id=str(row["job_id"]),
            chat_id=int(row["chat_id"]),
            message_id=int(row["message_id"]),
            replacement_count=int(row["replacement_count"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def save_job_display_message(self, ref: JobDisplayMessage) -> JobDisplayMessage:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO job_display_messages(job_id, chat_id, message_id, replacement_count)
                VALUES(?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    message_id=excluded.message_id,
                    replacement_count=excluded.replacement_count,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (ref.job_id, ref.chat_id, ref.message_id, ref.replacement_count),
            )
        loaded = await self.get_job_display_message(ref.job_id)
        if loaded is None:
            raise RuntimeError("job display message disappeared after save")
        return loaded

    @staticmethod
    def _collection_session_from_row(row: aiosqlite.Row) -> CollectionSession:
        return CollectionSession(
            id=str(row["id"]),
            owner_id=int(row["owner_id"]),
            chat_id=int(row["chat_id"]),
            state=CollectionState(row["state"]),
            status_chat_id=row["status_chat_id"],
            status_message_id=row["status_message_id"],
            finalized_job_ids=tuple(json.loads(row["finalized_job_ids_json"] or "[]")),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _collection_entry_from_row(row: aiosqlite.Row) -> CollectionEntry:
        return CollectionEntry(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            ordinal=int(row["ordinal"]),
            kind=CollectionEntryKind(row["entry_kind"]),
            source_chat_id=row["source_chat_id"],
            source_message_id=row["source_message_id"],
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
        )
