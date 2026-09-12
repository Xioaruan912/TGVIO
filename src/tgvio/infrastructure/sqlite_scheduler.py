from __future__ import annotations

from tgvio.domain.job import JobState
from tgvio.domain.scheduler import PhaseClaim, PublishGate, RuntimeLease


class SQLiteSchedulerRepositoryMixin:
    @staticmethod
    async def _unix_now(conn) -> int:
        cursor = await conn.execute("SELECT CAST(strftime('%s','now') AS INTEGER)")
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RuntimeError("SQLite did not return current time")
        return int(row[0])

    async def acquire_runtime_lease(
        self,
        lease_name: str,
        holder_id: str,
        *,
        ttl_seconds: int,
    ) -> RuntimeLease | None:
        ttl = max(3, int(ttl_seconds))
        async with self._write_transaction() as conn:
            now = await self._unix_now(conn)
            cursor = await conn.execute(
                "SELECT holder_id, generation, expires_at FROM runtime_leases WHERE lease_name=?",
                (lease_name,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                generation = 1
                expires_at = now + ttl
                await conn.execute(
                    """
                    INSERT INTO runtime_leases(
                        lease_name, holder_id, generation, acquired_at, heartbeat_at, expires_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (lease_name, holder_id, generation, now, now, expires_at),
                )
                return RuntimeLease(lease_name, holder_id, generation, expires_at)
            if int(row["expires_at"]) > now:
                return None
            generation = int(row["generation"]) + 1
            expires_at = now + ttl
            cursor = await conn.execute(
                """
                UPDATE runtime_leases
                SET holder_id=?, generation=?, acquired_at=?, heartbeat_at=?, expires_at=?
                WHERE lease_name=? AND generation=? AND expires_at<=?
                """,
                (
                    holder_id,
                    generation,
                    now,
                    now,
                    expires_at,
                    lease_name,
                    int(row["generation"]),
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return RuntimeLease(lease_name, holder_id, generation, expires_at)

    async def heartbeat_runtime_lease(
        self,
        lease: RuntimeLease,
        *,
        ttl_seconds: int,
    ) -> RuntimeLease | None:
        ttl = max(3, int(ttl_seconds))
        async with self._write_transaction() as conn:
            now = await self._unix_now(conn)
            expires_at = now + ttl
            cursor = await conn.execute(
                """
                UPDATE runtime_leases
                SET heartbeat_at=?, expires_at=?
                WHERE lease_name=? AND holder_id=? AND generation=? AND expires_at>?
                """,
                (
                    now,
                    expires_at,
                    lease.lease_name,
                    lease.holder_id,
                    lease.generation,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return RuntimeLease(
                lease.lease_name,
                lease.holder_id,
                lease.generation,
                expires_at,
            )

    async def release_runtime_lease(self, lease: RuntimeLease) -> bool:
        async with self._write_transaction() as conn:
            now = await self._unix_now(conn)
            cursor = await conn.execute(
                """
                UPDATE runtime_leases
                SET heartbeat_at=?, expires_at=0
                WHERE lease_name=? AND holder_id=? AND generation=?
                """,
                (now, lease.lease_name, lease.holder_id, lease.generation),
            )
            return cursor.rowcount == 1

    async def acquire_phase_claim(
        self,
        job_id: str,
        phase: str,
        holder_id: str,
        *,
        ttl_seconds: int,
    ) -> PhaseClaim | None:
        if not phase or len(phase) > 40:
            raise ValueError("invalid claim phase")
        ttl = max(3, int(ttl_seconds))
        async with self._write_transaction() as conn:
            now = await self._unix_now(conn)
            cursor = await conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,))
            if await cursor.fetchone() is None:
                await cursor.close()
                raise KeyError(f"job not found: {job_id}")
            await cursor.close()
            cursor = await conn.execute(
                """
                SELECT holder_id, generation, expires_at
                FROM job_phase_claims
                WHERE job_id=? AND phase=?
                """,
                (job_id, phase),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                generation = 1
                expires_at = now + ttl
                await conn.execute(
                    """
                    INSERT INTO job_phase_claims(
                        job_id, phase, holder_id, generation, claimed_at, heartbeat_at, expires_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (job_id, phase, holder_id, generation, now, now, expires_at),
                )
                return PhaseClaim(job_id, phase, holder_id, generation, expires_at)
            if int(row["expires_at"]) > now:
                return None
            generation = int(row["generation"]) + 1
            expires_at = now + ttl
            cursor = await conn.execute(
                """
                UPDATE job_phase_claims
                SET holder_id=?, generation=?, claimed_at=?, heartbeat_at=?, expires_at=?
                WHERE job_id=? AND phase=? AND generation=? AND expires_at<=?
                """,
                (
                    holder_id,
                    generation,
                    now,
                    now,
                    expires_at,
                    job_id,
                    phase,
                    int(row["generation"]),
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return PhaseClaim(job_id, phase, holder_id, generation, expires_at)

    async def heartbeat_phase_claim(
        self,
        claim: PhaseClaim,
        *,
        ttl_seconds: int,
    ) -> PhaseClaim | None:
        ttl = max(3, int(ttl_seconds))
        async with self._write_transaction() as conn:
            now = await self._unix_now(conn)
            expires_at = now + ttl
            cursor = await conn.execute(
                """
                UPDATE job_phase_claims
                SET heartbeat_at=?, expires_at=?
                WHERE job_id=? AND phase=? AND holder_id=? AND generation=? AND expires_at>?
                """,
                (
                    now,
                    expires_at,
                    claim.job_id,
                    claim.phase,
                    claim.holder_id,
                    claim.generation,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return PhaseClaim(
                claim.job_id,
                claim.phase,
                claim.holder_id,
                claim.generation,
                expires_at,
            )

    async def release_phase_claim(self, claim: PhaseClaim) -> bool:
        async with self._write_transaction() as conn:
            now = await self._unix_now(conn)
            cursor = await conn.execute(
                """
                UPDATE job_phase_claims
                SET heartbeat_at=?, expires_at=0
                WHERE job_id=? AND phase=? AND holder_id=? AND generation=?
                """,
                (
                    now,
                    claim.job_id,
                    claim.phase,
                    claim.holder_id,
                    claim.generation,
                ),
            )
            return cursor.rowcount == 1

    async def get_accepted_order(self, job_id: str) -> int | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT accepted_order FROM job_schedule WHERE job_id=?",
            (job_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else int(row["accepted_order"])

    async def get_next_publish_gate(self) -> PublishGate | None:
        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT s.job_id, s.accepted_order, j.state, j.error_code
            FROM job_schedule s
            JOIN jobs j ON j.id=s.job_id
            WHERE j.state NOT IN ('succeeded','cancelled')
              AND NOT (
                j.state='failed'
                AND (
                    COALESCE(j.error_code, '') NOT IN ('publish_partial','publish_uncertain')
                    OR COALESCE(
                        json_extract(j.policy_json, '$.auto_recovery_job.status'),
                        ''
                    )='quarantined'
                )
              )
            ORDER BY s.accepted_order
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return PublishGate(
            job_id=str(row["job_id"]),
            accepted_order=int(row["accepted_order"]),
            state=JobState(row["state"]),
            error_code=row["error_code"],
        )
