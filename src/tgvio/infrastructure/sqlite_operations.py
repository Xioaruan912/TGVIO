from __future__ import annotations

import json

import aiosqlite

from tgvio.domain.operations import OperationToken


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
