from __future__ import annotations

import time
from typing import TYPE_CHECKING

from tgvio.domain.collection_editing import (
    CollectionDraft,
    CollectionSubmission,
    DraftEntry,
    DraftState,
    EditingField,
    EditingInteraction,
)

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

    from tgvio.domain.intake import CollectionEntry


_EDITABLE_STATES = ("collecting", "preview", "saved")


class SQLiteCollectionEditingRepositoryMixin:
    # ---------------------------------------------------------------- drafts
    async def get_draft(self, session_id: str) -> CollectionDraft | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM collection_drafts WHERE session_id=?", (str(session_id),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._draft_from_row(row)

    async def get_active_draft(self, owner_id: int, chat_id: int) -> CollectionDraft | None:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT * FROM collection_drafts
            WHERE owner_id=? AND chat_id=? AND active=1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (int(owner_id), int(chat_id)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._draft_from_row(row)

    async def list_drafts(self, owner_id: int, *, limit: int = 20) -> list[CollectionDraft]:
        conn = self._require()
        cursor = await conn.execute(
            f"""
            SELECT * FROM collection_drafts
            WHERE owner_id=? AND editor_state IN ({",".join("?" for _ in _EDITABLE_STATES)})
            ORDER BY updated_at DESC, session_id
            LIMIT ?
            """,
            (int(owner_id), *_EDITABLE_STATES, max(1, int(limit))),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._draft_from_row(row) for row in rows]

    async def create_draft(
        self,
        session_id: str,
        owner_id: int,
        chat_id: int,
        *,
        style_json: str | None = None,
    ) -> CollectionDraft:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO collection_drafts(
                    session_id, owner_id, chat_id, revision, editor_state, active, style_json
                ) VALUES(?,?,?,1,'collecting',1,?)
                """,
                (str(session_id), int(owner_id), int(chat_id), style_json),
            )
        draft = await self.get_draft(session_id)
        if draft is None:
            raise RuntimeError("collection draft disappeared after create")
        return draft

    async def mark_draft_submitted(self, session_id: str) -> None:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE collection_drafts
                SET editor_state='submitted', active=0, updated_at=CURRENT_TIMESTAMP
                WHERE session_id=?
                """,
                (str(session_id),),
            )

    async def mark_draft_discarded(self, session_id: str) -> None:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE collection_drafts
                SET editor_state='discarded', active=0, updated_at=CURRENT_TIMESTAMP
                WHERE session_id=?
                """,
                (str(session_id),),
            )

    async def save_draft(self, session_id: str, *, expected_revision: int) -> CollectionDraft | None:
        return await self._bump_draft(
            session_id,
            expected_revision=expected_revision,
            active=0,
            editor_state=DraftState.SAVED.value,
        )

    async def activate_draft(
        self,
        session_id: str,
        *,
        expected_revision: int | None = None,
    ) -> CollectionDraft | None:
        """Re-open a saved draft as the single active collecting draft."""
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM collection_drafts WHERE session_id=?", (str(session_id),)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            if expected_revision is not None and int(row["revision"]) != int(expected_revision):
                return None
            session_id_value = str(row["session_id"])
            await conn.execute(
                """
                UPDATE collection_drafts SET active=0, updated_at=CURRENT_TIMESTAMP
                WHERE owner_id=? AND chat_id=? AND active=1 AND session_id!=?
                """,
                (int(row["owner_id"]), int(row["chat_id"]), session_id_value),
            )
            await conn.execute(
                """
                UPDATE collection_drafts
                SET active=1,
                    editor_state=CASE WHEN editor_state='saved' THEN 'collecting' ELSE editor_state END,
                    revision=revision+1,
                    updated_at=CURRENT_TIMESTAMP
                WHERE session_id=?
                """,
                (session_id_value,),
            )
        return await self.get_draft(session_id)

    async def set_draft_cover(
        self,
        session_id: str,
        *,
        entry_id: int | None,
        expected_revision: int,
    ) -> CollectionDraft | None:
        return await self._bump_draft(
            session_id,
            expected_revision=expected_revision,
            cover_entry_id=None if entry_id is None else int(entry_id),
        )

    async def set_draft_caption(
        self,
        session_id: str,
        *,
        text: str | None,
        expected_revision: int,
    ) -> CollectionDraft | None:
        return await self._bump_draft(
            session_id,
            expected_revision=expected_revision,
            caption_override=text,
        )

    async def set_draft_style(
        self,
        session_id: str,
        *,
        style_json: str | None,
        expected_revision: int,
    ) -> CollectionDraft | None:
        return await self._bump_draft(
            session_id,
            expected_revision=expected_revision,
            style_json=style_json,
        )

    async def _bump_draft(
        self,
        session_id: str,
        *,
        expected_revision: int,
        **fields: object,
    ) -> CollectionDraft | None:
        assignments = ["revision=revision+1", "updated_at=CURRENT_TIMESTAMP"]
        assignments.extend(f"{key}=?" for key in fields)
        values = list(fields.values())
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                f"""
                UPDATE collection_drafts
                SET {", ".join(assignments)}
                WHERE session_id=? AND revision=? AND editor_state IN ('collecting','preview','saved')
                """,
                (*values, str(session_id), int(expected_revision)),
            )
            if cursor.rowcount != 1:
                return None
        return await self.get_draft(session_id)

    # --------------------------------------------------------------- entries
    async def ensure_entry_edits(self, session_id: str) -> None:
        async with self._write_transaction() as conn:
            await self._seed_entry_edits(conn, session_id)

    async def _seed_entry_edits(self, conn: "aiosqlite.Connection", session_id: str) -> None:
        await conn.execute(
            """
            INSERT OR IGNORE INTO collection_entry_edits(entry_id, session_id, position, excluded)
            SELECT id, session_id, ordinal, 0 FROM collection_entries WHERE session_id=?
            """,
            (str(session_id),),
        )

    async def _next_edit_position(
        self, conn: "aiosqlite.Connection", session_id: str
    ) -> int:
        cursor = await conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS max_position FROM collection_entry_edits WHERE session_id=?",
            (str(session_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return (int(row["max_position"]) if row is not None else -1) + 1

    async def list_draft_entries(self, session_id: str) -> list[DraftEntry]:
        await self.ensure_entry_edits(session_id)
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT e.*, COALESCE(ed.position, e.ordinal) AS edit_position,
                   COALESCE(ed.excluded, 0) AS edit_excluded
            FROM collection_entries e
            LEFT JOIN collection_entry_edits ed ON ed.entry_id=e.id
            WHERE e.session_id=?
            ORDER BY edit_position, e.ordinal, e.id
            """,
            (str(session_id),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        entries: list[DraftEntry] = []
        for row in rows:
            entry: CollectionEntry = self._collection_entry_from_row(row)
            entries.append(
                DraftEntry(
                    entry=entry,
                    position=int(row["edit_position"]),
                    excluded=bool(row["edit_excluded"]),
                )
            )
        return entries

    async def set_entry_excluded(
        self,
        session_id: str,
        entry_id: int,
        *,
        excluded: bool,
        expected_revision: int,
    ) -> CollectionDraft | None:
        draft = await self._bump_draft(session_id, expected_revision=expected_revision)
        if draft is None:
            return None
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE collection_entry_edits SET excluded=?
                WHERE entry_id=? AND session_id=?
                """,
                (1 if excluded else 0, int(entry_id), str(session_id)),
            )
        return draft

    async def move_draft_entry(
        self,
        session_id: str,
        entry_id: int,
        *,
        direction: int,
        expected_revision: int,
    ) -> CollectionDraft | None:
        entries = await self.list_draft_entries(session_id)
        visible = [item for item in entries if not item.excluded and item.entry.id is not None]
        index = next(
            (pos for pos, item in enumerate(visible) if int(item.entry.id) == int(entry_id)),
            None,
        )
        if index is None:
            return None
        target = index + (1 if direction >= 0 else -1)
        if target < 0 or target >= len(visible):
            return None
        draft = await self._bump_draft(session_id, expected_revision=expected_revision)
        if draft is None:
            return None
        left = visible[index]
        right = visible[target]
        async with self._write_transaction() as conn:
            await conn.execute(
                "UPDATE collection_entry_edits SET position=? WHERE entry_id=?",
                (int(right.position), int(left.entry.id)),
            )
            await conn.execute(
                "UPDATE collection_entry_edits SET position=? WHERE entry_id=?",
                (int(left.position), int(right.entry.id)),
            )
        return draft

    # ----------------------------------------------------------- submissions
    async def get_submission(self, session_id: str) -> CollectionSubmission | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM collection_submissions WHERE session_id=?", (str(session_id),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._submission_from_row(row)

    async def get_submission_by_token(self, token_id: str) -> CollectionSubmission | None:
        if not token_id:
            return None
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM collection_submissions WHERE token_id=? LIMIT 1", (str(token_id),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._submission_from_row(row)

    async def commit_frozen_submission(
        self,
        *,
        session_id: str,
        owner_id: int,
        revision: int,
        snapshot_hash: str,
        frozen_json: str,
        token_id: str,
        token: str,
        action: str,
        resource_type: str,
        payload_hash: str,
    ) -> tuple[CollectionSubmission, bool]:
        """Atomically authorize + persist the frozen submission.

        Verifies the draft revision is unchanged, consumes the operation token with
        a payload/revision CAS, invalidates sibling tokens and inserts the frozen
        submission in ONE transaction. Any failure leaves no submission behind.
        """
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM collection_submissions WHERE session_id=?", (str(session_id),)
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is not None:
                return self._submission_from_row(existing), False

            cursor = await conn.execute(
                "SELECT revision, editor_state FROM collection_drafts WHERE session_id=?",
                (str(session_id),),
            )
            draft = await cursor.fetchone()
            await cursor.close()
            if (
                draft is None
                or int(draft["revision"]) != int(revision)
                or str(draft["editor_state"]) not in {"collecting", "preview", "saved"}
            ):
                raise ValueError("draft revision changed")

            cursor = await conn.execute(
                """
                UPDATE operation_tokens
                SET consumed_at=CAST(strftime('%s','now') AS INTEGER)
                WHERE token=? AND owner_id=? AND action=? AND resource_type=?
                  AND resource_id=? AND expected_revision=? AND payload_hash=?
                  AND consumed_at IS NULL
                  AND expires_at > CAST(strftime('%s','now') AS INTEGER)
                """,
                (
                    str(token),
                    int(owner_id),
                    str(action),
                    str(resource_type),
                    str(session_id),
                    int(revision),
                    str(payload_hash),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("operation token invalid")
            await conn.execute(
                """
                UPDATE operation_tokens
                SET consumed_at=CAST(strftime('%s','now') AS INTEGER)
                WHERE token<>? AND owner_id=? AND action=? AND resource_type=?
                  AND resource_id=? AND consumed_at IS NULL
                """,
                (str(token), int(owner_id), str(action), str(resource_type), str(session_id)),
            )
            await conn.execute(
                """
                INSERT INTO collection_submissions(
                    session_id, owner_id, revision, snapshot_hash,
                    job_ids_json, state, frozen_json, token_id
                ) VALUES(?,?,?,?,'[]','creating',?,?)
                """,
                (
                    str(session_id),
                    int(owner_id),
                    int(revision),
                    str(snapshot_hash),
                    str(frozen_json),
                    str(token_id),
                ),
            )
        created = await self.get_submission(session_id)
        if created is None:
            raise RuntimeError("submission disappeared after commit")
        return created, True

    async def begin_submission(
        self,
        session_id: str,
        *,
        owner_id: int,
        revision: int,
        snapshot_hash: str,
    ) -> tuple[CollectionSubmission, bool]:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM collection_submissions WHERE session_id=?", (str(session_id),)
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is not None:
                return self._submission_from_row(existing), False
            await conn.execute(
                """
                INSERT INTO collection_submissions(
                    session_id, owner_id, revision, snapshot_hash, job_ids_json, state
                ) VALUES(?,?,?,?,'[]','creating')
                """,
                (str(session_id), int(owner_id), int(revision), str(snapshot_hash)),
            )
        created = await self.get_submission(session_id)
        if created is None:
            raise RuntimeError("submission disappeared after create")
        return created, True

    async def append_submission_job(self, session_id: str, job_id: str) -> None:
        import json

        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT job_ids_json FROM collection_submissions WHERE session_id=?",
                (str(session_id),),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return
            try:
                job_ids = [str(value) for value in json.loads(row["job_ids_json"] or "[]")]
            except (TypeError, ValueError):
                job_ids = []
            if str(job_id) not in job_ids:
                job_ids.append(str(job_id))
            await conn.execute(
                """
                UPDATE collection_submissions
                SET job_ids_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE session_id=?
                """,
                (json.dumps(job_ids, separators=(",", ":")), str(session_id)),
            )

    async def record_collection_part_job(
        self, session_id: str, part_index: int, job_id: str
    ) -> None:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO collection_part_jobs(session_id, part_index, job_id)
                VALUES(?,?,?)
                """,
                (str(session_id), int(part_index), str(job_id)),
            )

    async def get_collection_part_job(self, session_id: str, part_index: int) -> str | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT job_id FROM collection_part_jobs WHERE session_id=? AND part_index=?",
            (str(session_id), int(part_index)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else str(row["job_id"])

    async def finish_submission(
        self,
        session_id: str,
        *,
        job_ids: tuple[str, ...],
        state: str = "created",
    ) -> None:
        import json

        async with self._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE collection_submissions
                SET job_ids_json=?, state=?, updated_at=CURRENT_TIMESTAMP
                WHERE session_id=?
                """,
                (json.dumps(list(job_ids), separators=(",", ":")), str(state), str(session_id)),
            )

    # ---------------------------------------------------------- interactions
    async def create_editing_interaction(
        self,
        interaction: EditingInteraction,
    ) -> EditingInteraction:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO editing_interactions(
                    id, owner_id, chat_id, session_id, field, expected_revision, expires_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    interaction.id,
                    int(interaction.owner_id),
                    int(interaction.chat_id),
                    interaction.session_id,
                    interaction.field.value,
                    int(interaction.expected_revision),
                    int(interaction.expires_at),
                ),
            )
        return interaction

    async def get_active_editing_interaction(
        self,
        owner_id: int,
        chat_id: int,
    ) -> EditingInteraction | None:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT * FROM editing_interactions
            WHERE owner_id=? AND chat_id=? AND consumed_at IS NULL AND expires_at>?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (int(owner_id), int(chat_id), int(time.time())),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._interaction_from_row(row)

    async def consume_editing_interaction(
        self,
        interaction_id: str,
        *,
        owner_id: int,
        expected_revision: int,
    ) -> bool:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE editing_interactions
                SET consumed_at=?
                WHERE id=? AND owner_id=? AND expected_revision=?
                  AND consumed_at IS NULL AND expires_at>?
                """,
                (
                    int(time.time()),
                    str(interaction_id),
                    int(owner_id),
                    int(expected_revision),
                    int(time.time()),
                ),
            )
            return cursor.rowcount == 1

    # -------------------------------------------------------- overlay snapshots
    async def overlay_snapshot(self, session_id: str) -> dict[str, object]:
        entries = await self.list_draft_entries(session_id)
        draft = await self.get_draft(session_id)
        return {
            "positions": {
                str(item.entry.id): int(item.position)
                for item in entries
                if item.entry.id is not None
            },
            "excluded": {
                str(item.entry.id): 1 if item.excluded else 0
                for item in entries
                if item.entry.id is not None
            },
            "cover_entry_id": None if draft is None else draft.cover_entry_id,
        }

    async def apply_overlay(
        self,
        session_id: str,
        overlay: dict[str, object],
        *,
        expected_revision: int,
    ) -> CollectionDraft | None:
        positions = overlay.get("positions") or {}
        excluded = overlay.get("excluded") or {}
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE collection_drafts
                SET revision=revision+1, updated_at=CURRENT_TIMESTAMP, cover_entry_id=?
                WHERE session_id=? AND revision=? AND editor_state IN ('collecting','preview','saved')
                """,
                (
                    overlay.get("cover_entry_id"),
                    str(session_id),
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                return None
            for entry_id, position in dict(positions).items():
                await conn.execute(
                    "UPDATE collection_entry_edits SET position=? WHERE entry_id=? AND session_id=?",
                    (int(position), int(entry_id), str(session_id)),
                )
            for entry_id, flag in dict(excluded).items():
                await conn.execute(
                    "UPDATE collection_entry_edits SET excluded=? WHERE entry_id=? AND session_id=?",
                    (int(flag), int(entry_id), str(session_id)),
                )
        return await self.get_draft(session_id)


    # ----------------------------------------------------------------- mappers
    @staticmethod
    def _draft_from_row(row) -> CollectionDraft:
        return CollectionDraft(
            session_id=str(row["session_id"]),
            owner_id=int(row["owner_id"]),
            chat_id=int(row["chat_id"]),
            revision=int(row["revision"]),
            state=DraftState(str(row["editor_state"])),
            cover_entry_id=(
                None if row["cover_entry_id"] is None else int(row["cover_entry_id"])
            ),
            caption_override=row["caption_override"],
            style_json=(row["style_json"] if "style_json" in row.keys() else None),
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _submission_from_row(row) -> CollectionSubmission:
        import json

        try:
            job_ids = tuple(str(value) for value in json.loads(row["job_ids_json"] or "[]"))
        except (TypeError, ValueError):
            job_ids = ()
        return CollectionSubmission(
            session_id=str(row["session_id"]),
            owner_id=int(row["owner_id"]),
            revision=int(row["revision"]),
            snapshot_hash=str(row["snapshot_hash"]),
            job_ids=job_ids,
            state=str(row["state"]),
            token_id=(row["token_id"] if "token_id" in row.keys() else None),
            frozen_json=(row["frozen_json"] if "frozen_json" in row.keys() else None),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _interaction_from_row(row) -> EditingInteraction:
        return EditingInteraction(
            id=str(row["id"]),
            owner_id=int(row["owner_id"]),
            chat_id=int(row["chat_id"]),
            session_id=str(row["session_id"]),
            field=EditingField(str(row["field"])),
            expected_revision=int(row["expected_revision"]),
            expires_at=int(row["expires_at"]),
            consumed_at=None if row["consumed_at"] is None else int(row["consumed_at"]),
            created_at=row["created_at"],
        )
