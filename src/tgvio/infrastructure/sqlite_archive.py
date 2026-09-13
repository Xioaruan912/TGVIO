from __future__ import annotations

import json

import aiosqlite

from tgvio.domain.archive import (
    ARCHIVE_OBJECT_TRANSITIONS,
    ARCHIVE_PACKAGE_TRANSITIONS,
    ArchiveEvent,
    ArchiveObject,
    ArchiveObjectRole,
    ArchiveObjectState,
    ArchivePackage,
    ArchivePackageState,
    ArchivePlan,
    ArchivePolicy,
)


class SQLiteArchiveRepositoryMixin:
    async def save_archive_plan(self, plan: ArchivePlan) -> ArchivePackage:
        package = plan.package
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT state, archive_profile_id, archive_policy, archive_policy_version
                FROM archive_packages WHERE job_id=?
                """,
                (package.job_id,),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is not None and existing["state"] != ArchivePackageState.PLANNED.value:
                raise ValueError(
                    f"archive package cannot be replanned from state {existing['state']}"
                )
            if existing is not None and (
                str(existing["archive_profile_id"]) != package.archive_profile_id
                or str(existing["archive_policy"]) != package.archive_policy.value
                or int(existing["archive_policy_version"]) != package.archive_policy_version
            ):
                raise ValueError("archive package profile/policy snapshot is already frozen")
            if existing is not None:
                await conn.execute(
                    "DELETE FROM archive_packages WHERE job_id=?",
                    (package.job_id,),
                )
            await conn.execute(
                """
                INSERT INTO archive_packages(
                    id, job_id, layout_version, remote_path, staging_path,
                    state, manifest_json, manifest_sha256, error_code, error_message,
                    archive_profile_id, archive_policy, archive_policy_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    package.id,
                    package.job_id,
                    package.layout_version,
                    package.remote_path,
                    package.staging_path,
                    package.state.value,
                    json.dumps(package.manifest, ensure_ascii=False, separators=(",", ":")),
                    package.manifest_sha256,
                    package.error_code,
                    package.error_message,
                    package.archive_profile_id,
                    package.archive_policy.value,
                    package.archive_policy_version,
                ),
            )
            for obj in package.objects:
                await conn.execute(
                    """
                    INSERT INTO archive_objects(
                        package_id, object_index, item_index, role, local_path,
                        remote_relpath, size_bytes, sha256, state,
                        verification_method, remote_etag, retry_count,
                        next_retry_at, error_code, error_message
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        package.id,
                        obj.object_index,
                        obj.item_index,
                        obj.role.value,
                        obj.local_path,
                        obj.remote_relpath,
                        obj.size_bytes,
                        obj.sha256,
                        obj.state.value,
                        obj.verification_method,
                        obj.remote_etag,
                        obj.retry_count,
                        obj.next_retry_at,
                        obj.error_code,
                        obj.error_message,
                    ),
                )
            await conn.execute(
                """
                INSERT INTO archive_events(package_id, event_type, detail_json)
                VALUES(?,?,?)
                """,
                (
                    package.id,
                    "archive_planned",
                    json.dumps(plan.summary, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        loaded = await self.get_archive_package_for_job(package.job_id)
        if loaded is None:
            raise RuntimeError("archive package disappeared after save")
        return loaded

    async def get_archive_package_for_job(self, job_id: str) -> ArchivePackage | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM archive_packages WHERE job_id=?",
            (job_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        cursor = await conn.execute(
            "SELECT * FROM archive_objects WHERE package_id=? ORDER BY object_index",
            (row["id"],),
        )
        object_rows = await cursor.fetchall()
        await cursor.close()
        return self._archive_package_from_rows(row, object_rows)

    async def get_archive_package(self, package_id: str) -> ArchivePackage | None:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM archive_packages WHERE id=?",
            (package_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        cursor = await conn.execute(
            "SELECT * FROM archive_objects WHERE package_id=? ORDER BY object_index",
            (package_id,),
        )
        object_rows = await cursor.fetchall()
        await cursor.close()
        return self._archive_package_from_rows(row, object_rows)

    async def list_archive_packages_by_states(
        self,
        states: tuple[ArchivePackageState, ...],
        *,
        limit: int = 20,
    ) -> list[ArchivePackage]:
        if not states:
            return []
        conn = self._require()
        placeholders = ",".join("?" for _ in states)
        cursor = await conn.execute(
            f"""
            SELECT id FROM archive_packages
            WHERE state IN ({placeholders})
            ORDER BY created_at, id
            LIMIT ?
            """,
            (*tuple(state.value for state in states), max(1, min(100, int(limit)))),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        packages: list[ArchivePackage] = []
        for row in rows:
            package = await self.get_archive_package(str(row["id"]))
            if package is not None:
                packages.append(package)
        return packages

    async def list_failed_archive_packages_for_auto_recovery(
        self,
        *,
        limit: int = 100,
    ) -> list[ArchivePackage]:
        """Bound periodic recovery scans to opted-in packages needing a decision."""

        conn = self._require()
        cursor = await conn.execute(
            """
            SELECT a.id
            FROM archive_packages a
            JOIN jobs j ON j.id=a.job_id
            WHERE a.state='failed'
              AND json_extract(j.policy_json, '$.auto_recovery.version')=1
              AND json_extract(j.policy_json, '$.auto_recovery.enabled')=1
              AND (
                    COALESCE(
                        json_extract(j.policy_json, '$.auto_recovery_archive.status'),
                        ''
                    ) NOT IN ('abandoned','exhausted')
                    OR COALESCE(
                        json_extract(j.policy_json, '$.auto_recovery_archive.failure_id'),
                        ''
                    ) != ('event:' || COALESCE(
                        (
                            SELECT COALESCE(
                                MAX(CASE WHEN e.event_type='archive_failed' THEN e.id END),
                                MAX(e.id)
                            )
                            FROM archive_events e
                            WHERE e.package_id=a.id
                        ),
                        ''
                    ))
              )
            ORDER BY a.updated_at, a.id
            LIMIT ?
            """,
            (max(1, min(500, int(limit))),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        packages: list[ArchivePackage] = []
        for row in rows:
            package = await self.get_archive_package(str(row["id"]))
            if package is not None:
                packages.append(package)
        return packages

    async def count_archive_packages_by_state(
        self,
        *,
        owner_id: int | None = None,
    ) -> dict[ArchivePackageState, int]:
        conn = self._require()
        if owner_id is None:
            cursor = await conn.execute(
                "SELECT state, COUNT(*) AS count FROM archive_packages GROUP BY state"
            )
        else:
            cursor = await conn.execute(
                """
                SELECT a.state, COUNT(*) AS count
                FROM archive_packages a
                JOIN jobs j ON j.id=a.job_id
                WHERE j.owner_id=?
                GROUP BY a.state
                """,
                (owner_id,),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            ArchivePackageState(row["state"]): int(row["count"])
            for row in rows
        }

    async def list_recent_archive_packages(
        self,
        *,
        owner_id: int | None = None,
        limit: int = 5,
    ) -> list[ArchivePackage]:
        conn = self._require()
        bounded = max(1, min(50, int(limit)))
        if owner_id is None:
            cursor = await conn.execute(
                "SELECT id FROM archive_packages ORDER BY created_at DESC, id DESC LIMIT ?",
                (bounded,),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT a.id
                FROM archive_packages a
                JOIN jobs j ON j.id=a.job_id
                WHERE j.owner_id=?
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT ?
                """,
                (owner_id, bounded),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        packages: list[ArchivePackage] = []
        for row in rows:
            package = await self.get_archive_package(str(row["id"]))
            if package is not None:
                packages.append(package)
        return packages

    async def list_archive_events(self, package_id: str) -> list[ArchiveEvent]:
        conn = self._require()
        cursor = await conn.execute(
            "SELECT * FROM archive_events WHERE package_id=? ORDER BY id",
            (package_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            ArchiveEvent(
                id=int(row["id"]),
                package_id=row["package_id"],
                object_id=int(row["object_id"]) if row["object_id"] is not None else None,
                event_type=row["event_type"],
                detail=json.loads(row["detail_json"] or "{}"),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def update_archive_package_state(
        self,
        package_id: str,
        state: ArchivePackageState,
        *,
        event_type: str,
        detail: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        committed: bool = False,
    ) -> ArchivePackage:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT state FROM archive_packages WHERE id=?",
                (package_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(f"archive package not found: {package_id}")
            current = ArchivePackageState(row["state"])
            if state != current and state not in ARCHIVE_PACKAGE_TRANSITIONS[current]:
                raise ValueError(
                    f"illegal archive package transition: {current.value} -> {state.value}"
                )
            await conn.execute(
                """
                UPDATE archive_packages
                SET state=?, error_code=?, error_message=?,
                    committed_at=CASE
                        WHEN ? THEN COALESCE(committed_at, CURRENT_TIMESTAMP)
                        ELSE committed_at
                    END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    state.value,
                    error_code,
                    error_message,
                    int(committed),
                    package_id,
                ),
            )
            await conn.execute(
                """
                INSERT INTO archive_events(package_id, event_type, detail_json)
                VALUES(?,?,?)
                """,
                (
                    package_id,
                    event_type,
                    json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        loaded = await self.get_archive_package(package_id)
        if loaded is None:
            raise RuntimeError("archive package disappeared after state update")
        return loaded

    async def update_archive_object_state(
        self,
        object_id: int,
        state: ArchiveObjectState,
        *,
        event_type: str,
        verification_method: str | None = None,
        remote_etag: str | None = None,
        retry_count: int | None = None,
        next_retry_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> ArchiveObject:
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM archive_objects WHERE id=?",
                (object_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(f"archive object not found: {object_id}")
            current = ArchiveObjectState(row["state"])
            if state != current and state not in ARCHIVE_OBJECT_TRANSITIONS[current]:
                raise ValueError(
                    f"illegal archive object transition: {current.value} -> {state.value}"
                )
            await conn.execute(
                """
                UPDATE archive_objects
                SET state=?, verification_method=COALESCE(?, verification_method),
                    remote_etag=COALESCE(?, remote_etag),
                    retry_count=COALESCE(?, retry_count), next_retry_at=?,
                    error_code=?, error_message=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    state.value,
                    verification_method,
                    remote_etag,
                    retry_count,
                    next_retry_at,
                    error_code,
                    error_message,
                    object_id,
                ),
            )
            await conn.execute(
                """
                INSERT INTO archive_events(package_id, object_id, event_type, detail_json)
                VALUES(?,?,?,?)
                """,
                (
                    str(row["package_id"]),
                    object_id,
                    event_type,
                    json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            cursor = await conn.execute(
                "SELECT * FROM archive_objects WHERE id=?",
                (object_id,),
            )
            updated = await cursor.fetchone()
            await cursor.close()
        if updated is None:
            raise RuntimeError("archive object disappeared after state update")
        return self._archive_object_from_row(updated)

    @classmethod
    def _archive_package_from_rows(
        cls,
        row: aiosqlite.Row,
        object_rows: list[aiosqlite.Row],
    ) -> ArchivePackage:
        objects = tuple(cls._archive_object_from_row(obj) for obj in object_rows)
        return ArchivePackage(
            id=str(row["id"]),
            job_id=str(row["job_id"]),
            layout_version=str(row["layout_version"]),
            remote_path=str(row["remote_path"]),
            staging_path=str(row["staging_path"]),
            state=ArchivePackageState(row["state"]),
            manifest=json.loads(row["manifest_json"] or "{}"),
            objects=objects,
            manifest_sha256=row["manifest_sha256"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            committed_at=row["committed_at"],
            archive_profile_id=str(row["archive_profile_id"]),
            archive_policy=ArchivePolicy(row["archive_policy"]),
            archive_policy_version=int(row["archive_policy_version"]),
        )

    @staticmethod
    def _archive_object_from_row(row: aiosqlite.Row) -> ArchiveObject:
        return ArchiveObject(
            id=int(row["id"]),
            package_id=str(row["package_id"]),
            object_index=int(row["object_index"]),
            item_index=int(row["item_index"]),
            role=ArchiveObjectRole(row["role"]),
            local_path=str(row["local_path"]),
            remote_relpath=str(row["remote_relpath"]),
            size_bytes=int(row["size_bytes"]),
            sha256=str(row["sha256"]),
            state=ArchiveObjectState(row["state"]),
            verification_method=row["verification_method"],
            remote_etag=row["remote_etag"],
            retry_count=int(row["retry_count"]),
            next_retry_at=row["next_retry_at"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            updated_at=row["updated_at"],
        )
