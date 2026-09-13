from __future__ import annotations

import json

import aiosqlite

from tgvio.domain.operations import (
    OperationToken,
    PublishEffectRevocation,
    RevocationState,
)


class SQLiteOperationRepositoryMixin:
    async def create_operation_token(
        self,
        *,
        token: str,
        owner_id: int,
        action: str,
        resource_type: str,
        resource_id: str,
        expected_revision: int,
        payload_hash: str,
        payload: dict[str, object],
        ttl_seconds: int,
    ) -> OperationToken:
        ttl = max(1, min(3600, int(ttl_seconds)))
        async with self._write_transaction() as conn:
            now = "CAST(strftime('%s','now') AS INTEGER)"
            await conn.execute(
                f"""
                DELETE FROM operation_tokens
                WHERE (consumed_at IS NOT NULL AND consumed_at < {now} - 86400)
                   OR expires_at < {now} - 86400
                """
            )
            await conn.execute(
                f"""
                INSERT INTO operation_tokens(
                    token, owner_id, action, resource_type, resource_id,
                    expected_revision, payload_hash, payload_json, expires_at
                ) VALUES(?,?,?,?,?,?,?,?,{now} + ?)
                """,
                (
                    token,
                    int(owner_id),
                    action,
                    resource_type,
                    resource_id,
                    int(expected_revision),
                    payload_hash,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ttl,
                ),
            )
        created = await self.get_operation_token(token)
        if created is None:
            raise RuntimeError("operation token disappeared after insert")
        return created

    async def get_operation_token(self, token: str) -> OperationToken | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM operation_tokens WHERE token=?",
            (token,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._operation_token_from_row(row)

    async def consume_operation_token(
        self,
        *,
        token: str,
        owner_id: int,
        action: str,
        resource_type: str,
        resource_id: str,
        expected_revision: int,
        payload_hash: str,
    ) -> OperationToken | None:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE operation_tokens
                SET consumed_at=CAST(strftime('%s','now') AS INTEGER)
                WHERE token=?
                  AND owner_id=?
                  AND action=?
                  AND resource_type=?
                  AND resource_id=?
                  AND expected_revision=?
                  AND payload_hash=?
                  AND consumed_at IS NULL
                  AND expires_at > CAST(strftime('%s','now') AS INTEGER)
                """,
                (
                    token,
                    int(owner_id),
                    action,
                    resource_type,
                    resource_id,
                    int(expected_revision),
                    payload_hash,
                ),
            )
            if cursor.rowcount != 1:
                return None
            await conn.execute(
                """
                UPDATE operation_tokens
                SET consumed_at=CAST(strftime('%s','now') AS INTEGER)
                WHERE token<>?
                  AND owner_id=?
                  AND action=?
                  AND resource_type=?
                  AND resource_id=?
                  AND consumed_at IS NULL
                """,
                (token, int(owner_id), action, resource_type, resource_id),
            )
        return await self.get_operation_token(token)

    async def ensure_publish_effect_revocations(
        self,
        job_id: str,
        effect_ids: tuple[int, ...],
    ) -> None:
        ids = tuple(sorted(set(int(value) for value in effect_ids)))
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                f"""
                SELECT pe.id
                FROM publish_effects pe
                JOIN publish_plans pp ON pp.id=pe.plan_id
                WHERE pp.job_id=? AND pe.id IN ({placeholders})
                """,
                (job_id, *ids),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            owned = {int(row["id"]) for row in rows}
            if owned != set(ids):
                raise ValueError("publish effect does not belong to job")
            await conn.executemany(
                "INSERT OR IGNORE INTO publish_effect_revocations(effect_id) VALUES(?)",
                ((effect_id,) for effect_id in ids),
            )

    async def list_publish_effect_revocations(
        self,
        job_id: str,
    ) -> list[PublishEffectRevocation]:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT r.*
            FROM publish_effect_revocations r
            JOIN publish_effects pe ON pe.id=r.effect_id
            JOIN publish_plans pp ON pp.id=pe.plan_id
            WHERE pp.job_id=?
            ORDER BY r.effect_id
            """,
            (job_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._revocation_from_row(row) for row in rows]

    async def checkpoint_publish_effect_revocations(
        self,
        job_id: str,
        effect_ids: tuple[int, ...],
        *,
        state: RevocationState,
        error_code: str | None = None,
    ) -> list[PublishEffectRevocation]:
        if state not in {RevocationState.DELETED, RevocationState.FAILED}:
            raise ValueError("revocation checkpoint must be deleted or failed")
        ids = tuple(sorted(set(int(value) for value in effect_ids)))
        if not ids:
            return []
        await self.ensure_publish_effect_revocations(job_id, ids)
        safe_error = None if error_code is None else str(error_code)[:80]
        event_type = "delete_succeeded" if state == RevocationState.DELETED else "delete_failed"
        async with self._write_transaction() as conn:
            for effect_id in ids:
                cursor = await conn.execute(
                    """
                    UPDATE publish_effect_revocations
                    SET state=?,
                        attempt_count=attempt_count + 1,
                        error_code=?,
                        deleted_at=CASE WHEN ?='deleted' THEN CURRENT_TIMESTAMP ELSE deleted_at END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE effect_id=? AND state!='deleted'
                    """,
                    (state.value, safe_error, state.value, effect_id),
                )
                if cursor.rowcount == 0:
                    continue
                await conn.execute(
                    """
                    INSERT INTO publish_effect_revocation_events(effect_id, event_type, error_code)
                    VALUES(?,?,?)
                    """,
                    (effect_id, event_type, safe_error),
                )
        current = await self.list_publish_effect_revocations(job_id)
        by_id = {item.effect_id: item for item in current}
        return [by_id[effect_id] for effect_id in ids if effect_id in by_id]

    async def list_publish_effect_revocation_events(
        self,
        job_id: str,
    ) -> list[dict[str, object]]:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT e.effect_id, e.event_type, e.error_code, e.created_at
            FROM publish_effect_revocation_events e
            JOIN publish_effects pe ON pe.id=e.effect_id
            JOIN publish_plans pp ON pp.id=pe.plan_id
            WHERE pp.job_id=?
            ORDER BY e.id
            """,
            (job_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "effect_id": int(row["effect_id"]),
                "event_type": str(row["event_type"]),
                "error_code": row["error_code"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _operation_token_from_row(row: aiosqlite.Row) -> OperationToken:
        return OperationToken(
            token=str(row["token"]),
            owner_id=int(row["owner_id"]),
            action=str(row["action"]),
            resource_type=str(row["resource_type"]),
            resource_id=str(row["resource_id"]),
            expected_revision=int(row["expected_revision"]),
            payload_hash=str(row["payload_hash"]),
            payload=json.loads(row["payload_json"] or "{}"),
            expires_at=int(row["expires_at"]),
            consumed_at=(None if row["consumed_at"] is None else int(row["consumed_at"])),
            created_at=row["created_at"],
        )

    @staticmethod
    def _revocation_from_row(row: aiosqlite.Row) -> PublishEffectRevocation:
        return PublishEffectRevocation(
            effect_id=int(row["effect_id"]),
            state=RevocationState(row["state"]),
            attempt_count=int(row["attempt_count"]),
            error_code=row["error_code"],
            deleted_at=row["deleted_at"],
            updated_at=row["updated_at"],
        )
