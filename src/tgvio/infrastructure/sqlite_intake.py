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
    async def list_intake_source_ids(
        self,
        source_chat_id: int,
        *,
        exclude_states: tuple[str, ...] = ("failed", "cancelled"),
    ) -> set[int]:
        """Source message ids of one chat that are already attached to a job.

        Failed and cancelled jobs are excluded so those rows stay re-grabbable
        from the picker; everything else counts as "already submitted".
        """

        conn = self._require()
        params: list[object] = [int(source_chat_id)]
        clause = ""
        if exclude_states:
            placeholders = ",".join("?" for _ in exclude_states)
            clause = f" AND j.state NOT IN ({placeholders})"
            params.extend(str(state) for state in exclude_states)
        cursor = await conn.execute(
            f"""
            SELECT e.source_message_id AS source_message_id
            FROM intake_events e
            JOIN jobs j ON j.id = e.job_id
            WHERE e.source_chat_id = ?{clause}
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {int(row["source_message_id"]) for row in rows}

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
        *,
        release_keys: tuple[IntakeEventKey, ...] = (),
    ) -> bool:
        async with self._write_transaction() as conn:
            if release_keys:
                # A stale caller must not release a key that was revived or has
                # already produced a visible publish effect after its lookup.
                for start in range(0, len(release_keys), 400):
                    chunk = release_keys[start : start + 400]
                    placeholders = ",".join("(?,?)" for _ in chunk)
                    params: list[int] = []
                    for key in chunk:
                        params.extend((key.source_chat_id, key.source_message_id))
                    await conn.execute(
                        f"""
                        DELETE FROM intake_events
                        WHERE (source_chat_id, source_message_id) IN ({placeholders})
                          AND EXISTS (
                              SELECT 1 FROM jobs
                              WHERE jobs.id = intake_events.job_id
                                AND jobs.state IN ('failed', 'cancelled')
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM publish_plans
                              JOIN publish_effects
                                ON publish_effects.plan_id = publish_plans.id
                              WHERE publish_plans.job_id = intake_events.job_id
                          )
                        """,
                        tuple(params),
                    )
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
            SELECT s.*
            FROM collection_sessions s
            JOIN collection_drafts d ON d.session_id=s.id
            WHERE s.owner_id=? AND s.chat_id=? AND s.state='open' AND d.active=1
            ORDER BY s.created_at DESC
            LIMIT 1
            """,
            (owner_id, chat_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._collection_session_from_row(row)

    async def count_collection_entries(self, session_id: str) -> tuple[int, int]:
        cursor = await self._require().execute(
            "SELECT entry_kind, COUNT(*) AS count FROM collection_entries "
            "WHERE session_id=? GROUP BY entry_kind", (session_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        counts = {str(row["entry_kind"]): int(row["count"]) for row in rows}
        return counts.get("media", 0), counts.get("text", 0)

    async def get_collection(self, session_id: str) -> CollectionSession | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM collection_sessions WHERE id=?",
            (session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._collection_session_from_row(row)

    async def create_collection(
        self, session: CollectionSession, *, style_json: str | None = None
    ) -> CollectionSession:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT s.*
                FROM collection_sessions s
                JOIN collection_drafts d ON d.session_id=s.id
                WHERE s.owner_id=? AND s.chat_id=? AND s.state='open' AND d.active=1
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
            await conn.execute(
                """
                INSERT OR IGNORE INTO collection_drafts(
                    session_id, owner_id, chat_id, revision, editor_state, active, style_json
                ) VALUES(?,?,?,1,'collecting',1,?)
                """,
                (session.id, session.owner_id, session.chat_id, style_json),
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
            from tgvio.domain.intake import CollectionAlreadySubmittedError
            cursor = await conn.execute(
                "SELECT 1 FROM collection_submissions WHERE session_id=?", (session_id,),
            )
            submitted = await cursor.fetchone()
            await cursor.close()
            if submitted is not None:
                raise CollectionAlreadySubmittedError("collection already submitted")
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
            next_position = await self._next_edit_position(conn, session_id)
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
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO collection_entry_edits(
                        entry_id, session_id, position, excluded
                    ) VALUES(?,?,?,0)
                    """,
                    (int(cursor.lastrowid), session_id, next_position),
                )
                next_position += 1
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
            await conn.execute(
                """
                UPDATE collection_drafts
                SET editor_state='submitted', active=0, updated_at=CURRENT_TIMESTAMP
                WHERE session_id=?
                """,
                (session_id,),
            )
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
            await conn.execute(
                """
                UPDATE collection_drafts
                SET editor_state='discarded', active=0, updated_at=CURRENT_TIMESTAMP
                WHERE session_id=?
                """,
                (session_id,),
            )
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
        columns = row.keys()
        return UserPreference(
            owner_id=int(row["owner_id"]),
            spoiler_mode=SpoilerMode(row["spoiler_mode"]),
            quiet_mode=bool(row["quiet_mode"]) if "quiet_mode" in columns else False,
            style_json=(
                row["style_json"] if "style_json" in columns else None
            ),
            thumbnail_path=(
                row["thumbnail_path"] if "thumbnail_path" in columns else None
            ),
            caption_template=(
                row["caption_template"] if "caption_template" in columns else None
            ),
            ytdlp_preset=(
                row["ytdlp_preset"] if "ytdlp_preset" in columns else None
            ),
            ytdlp_audio_only=(
                bool(row["ytdlp_audio_only"])
                if "ytdlp_audio_only" in columns
                else False
            ),
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

    async def set_user_quiet_mode(self, owner_id: int, enabled: bool) -> UserPreference:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO user_preferences(owner_id, quiet_mode)
                VALUES(?,?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    quiet_mode=excluded.quiet_mode,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (int(owner_id), 1 if enabled else 0),
            )
        return await self.get_user_preference(owner_id)

    async def set_user_style(self, owner_id: int, style_json: str | None) -> UserPreference:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO user_preferences(owner_id, style_json)
                VALUES(?,?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    style_json=excluded.style_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (int(owner_id), style_json),
            )
        return await self.get_user_preference(owner_id)

    async def set_user_thumbnail_path(
        self, owner_id: int, thumbnail_path: str | None
    ) -> UserPreference:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO user_preferences(owner_id, thumbnail_path)
                VALUES(?,?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    thumbnail_path=excluded.thumbnail_path,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (int(owner_id), thumbnail_path),
            )
        return await self.get_user_preference(owner_id)

    async def set_user_caption_template(
        self, owner_id: int, caption_template: str | None
    ) -> UserPreference:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO user_preferences(owner_id, caption_template)
                VALUES(?,?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    caption_template=excluded.caption_template,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (int(owner_id), caption_template),
            )
        return await self.get_user_preference(owner_id)

    async def set_user_ytdlp_options(
        self,
        owner_id: int,
        *,
        preset: str,
        audio_only: bool,
    ) -> UserPreference:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO user_preferences(owner_id, ytdlp_preset, ytdlp_audio_only)
                VALUES(?,?,?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    ytdlp_preset=excluded.ytdlp_preset,
                    ytdlp_audio_only=excluded.ytdlp_audio_only,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (int(owner_id), preset, 1 if audio_only else 0),
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
