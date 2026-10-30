from __future__ import annotations



from tgvio.domain.intake import (
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
