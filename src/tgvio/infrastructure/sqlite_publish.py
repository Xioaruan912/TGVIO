from __future__ import annotations

import json

import aiosqlite

from tgvio.domain.job import MediaKind
from tgvio.domain.publish import (
    PublishEffect,
    PublishPlan,
    PublishStep,
    PublishStepKind,
    PublishStepState,
    PublishTarget,
)


class SQLitePublishRepositoryMixin:
    async def save_publish_plan(self, plan: PublishPlan) -> None:
        async with self._write_transaction() as conn:
            cursor = await conn.execute("SELECT 1 FROM jobs WHERE id=?", (plan.job_id,))
            exists = await cursor.fetchone()
            await cursor.close()
            if exists is None:
                raise KeyError(f"job not found: {plan.job_id}")
            await conn.execute("DELETE FROM publish_plans WHERE job_id=?", (plan.job_id,))
            await conn.execute(
                "INSERT INTO publish_plans(id, job_id, version, summary_json) VALUES(?,?,?,?)",
                (
                    plan.id,
                    plan.job_id,
                    plan.version,
                    json.dumps(plan.summary, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            for step in plan.steps:
                await conn.execute(
                    """
                    INSERT INTO publish_steps(
                        plan_id, step_index, kind, target, item_indexes_json,
                        params_json, state, error_code, error_message
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        plan.id,
                        step.index,
                        step.kind.value,
                        step.target.value,
                        json.dumps(list(step.item_indexes), separators=(",", ":")),
                        json.dumps(step.params, ensure_ascii=False, separators=(",", ":")),
                        step.state.value,
                        step.error_code,
                        step.error_message,
                    ),
                )

    async def get_publish_plan(self, job_id: str) -> PublishPlan | None:
        conn = self._require()
        cursor = await conn.execute("SELECT * FROM publish_plans WHERE job_id=?", (job_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        cursor = await conn.execute(
            "SELECT * FROM publish_steps WHERE plan_id=? ORDER BY step_index",
            (row["id"],),
        )
        step_rows = await cursor.fetchall()
        await cursor.close()
        steps = tuple(self._publish_step_from_row(step_row) for step_row in step_rows)
        return PublishPlan(
            id=row["id"],
            job_id=row["job_id"],
            version=int(row["version"]),
            summary=json.loads(row["summary_json"] or "{}"),
            steps=steps,
            created_at=row["created_at"],
        )

    async def update_publish_step_state(
        self,
        plan_id: str,
        step_index: int,
        state: PublishStepState,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> PublishStep:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE publish_steps
                SET state=?, error_code=?, error_message=?, updated_at=CURRENT_TIMESTAMP
                WHERE plan_id=? AND step_index=?
                """,
                (state.value, error_code, error_message, plan_id, step_index),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"publish step not found: {plan_id}:{step_index}")
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM publish_steps WHERE plan_id=? AND step_index=?",
            (plan_id, step_index),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RuntimeError("publish step disappeared after update")
        return self._publish_step_from_row(row)

    async def record_publish_effect(self, effect: PublishEffect) -> PublishEffect:
        recorded = await self.record_publish_effects((effect,))
        return recorded[0]

    async def record_publish_effects(
        self,
        effects: tuple[PublishEffect, ...],
    ) -> list[PublishEffect]:
        if not effects:
            return []
        effect_ids: list[int] = []
        async with self._write_transaction() as conn:
            for effect in effects:
                cursor = await conn.execute(
                    """
                    INSERT INTO publish_effects(
                        plan_id, step_index, effect_type, external_chat_id,
                        external_message_id, detail_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        effect.plan_id,
                        effect.step_index,
                        effect.effect_type,
                        effect.external_chat_id,
                        effect.external_message_id,
                        json.dumps(effect.detail, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                effect_ids.append(int(cursor.lastrowid))
        conn = self._require()
        placeholders = ",".join("?" for _ in effect_ids)
        cursor = await conn.execute(
            f"SELECT * FROM publish_effects WHERE id IN ({placeholders}) ORDER BY id",
            tuple(effect_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        if len(rows) != len(effect_ids):
            raise RuntimeError("publish effects disappeared after insert")
        return [self._publish_effect_from_row(row) for row in rows]

    async def list_publish_effects(self, plan_id: str) -> list[PublishEffect]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM publish_effects WHERE plan_id=? ORDER BY step_index, id",
            (plan_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._publish_effect_from_row(row) for row in rows]

    async def get_telegram_reference(
        self,
        sha256: str,
        destination: str,
        media_kind: MediaKind,
    ) -> str | None:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT reference
            FROM telegram_file_cache
            WHERE sha256=? AND destination=? AND media_kind=?
            """,
            (sha256, destination, media_kind.value),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return str(row["reference"]) if row is not None else None

    async def upsert_telegram_reference(
        self,
        sha256: str,
        destination: str,
        media_kind: MediaKind,
        reference: str,
    ) -> None:
        async with self._write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO telegram_file_cache(
                    sha256, destination, media_kind, reference
                ) VALUES(?,?,?,?)
                ON CONFLICT(sha256, destination, media_kind)
                DO UPDATE SET
                    reference=excluded.reference,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (sha256, destination, media_kind.value, reference),
            )

    @staticmethod
    def _publish_step_from_row(row: aiosqlite.Row) -> PublishStep:
        return PublishStep(
            index=int(row["step_index"]),
            kind=PublishStepKind(row["kind"]),
            target=PublishTarget(row["target"]),
            item_indexes=tuple(int(value) for value in json.loads(row["item_indexes_json"] or "[]")),
            params=json.loads(row["params_json"] or "{}"),
            state=PublishStepState(row["state"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    @staticmethod
    def _publish_effect_from_row(row: aiosqlite.Row) -> PublishEffect:
        return PublishEffect(
            id=int(row["id"]),
            plan_id=row["plan_id"],
            step_index=int(row["step_index"]),
            effect_type=row["effect_type"],
            external_chat_id=row["external_chat_id"],
            external_message_id=row["external_message_id"],
            detail=json.loads(row["detail_json"] or "{}"),
            created_at=row["created_at"],
        )
