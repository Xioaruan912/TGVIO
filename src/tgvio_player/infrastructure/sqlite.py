from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
from typing import AsyncIterator

from tgvio_player.domain.catalog import CatalogPackage
from tgvio_player.infrastructure.migration import run_migrations


class PlayerCatalogRepositorySQLite:
    """Player-owned SQLite catalog. It never opens the Bot state database."""

    def __init__(self, path: Path, *, migrations_dir: Path | None = None) -> None:
        self._path = path
        self._migrations_dir = migrations_dir or (
            Path(__file__).with_name("migrations")
        )
        self._conn: sqlite3.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        run_migrations(conn, self._migrations_dir)
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            conn.close()

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("player catalog repository is not open")
        return self._conn

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[sqlite3.Connection]:
        async with self._write_lock:
            conn = self._require()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    async def apply_package(self, package: CatalogPackage) -> None:
        async with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO catalog_packages(
                    package_id, remote_path, manifest_sha256,
                    manifest_etag, complete_etag, active, last_seen_at
                )
                VALUES(?,?,?,?,?,1,CURRENT_TIMESTAMP)
                ON CONFLICT(package_id) DO UPDATE SET
                    remote_path=excluded.remote_path,
                    manifest_sha256=excluded.manifest_sha256,
                    manifest_etag=excluded.manifest_etag,
                    complete_etag=excluded.complete_etag,
                    active=1,
                    last_seen_at=CURRENT_TIMESTAMP
                """,
                (
                    package.package_id,
                    package.remote_path,
                    package.manifest_sha256,
                    package.manifest_etag,
                    package.complete_etag,
                ),
            )
            conn.execute(
                "UPDATE media_locations SET active=0 WHERE package_id=?",
                (package.package_id,),
            )

            by_id = {item.media_id: item for item in package.media}
            for media in by_id.values():
                conn.execute(
                    """
                    INSERT INTO media(
                        media_id, kind, mime_type, size_bytes, width, height,
                        duration_seconds, container, codec, active,
                        first_seen_at, last_seen_at
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                    ON CONFLICT(media_id) DO UPDATE SET
                        kind=excluded.kind,
                        mime_type=COALESCE(excluded.mime_type, media.mime_type),
                        size_bytes=excluded.size_bytes,
                        width=COALESCE(excluded.width, media.width),
                        height=COALESCE(excluded.height, media.height),
                        duration_seconds=COALESCE(excluded.duration_seconds, media.duration_seconds),
                        container=COALESCE(excluded.container, media.container),
                        codec=COALESCE(excluded.codec, media.codec),
                        last_seen_at=CURRENT_TIMESTAMP
                    """,
                    (
                        media.media_id,
                        media.kind,
                        media.mime_type,
                        media.size_bytes,
                        media.width,
                        media.height,
                        media.duration_seconds,
                        media.container,
                        media.codec,
                    ),
                )

            for location in package.locations:
                conn.execute(
                    """
                    INSERT INTO media_locations(
                        package_id, media_id, remote_relpath,
                        remote_etag, active, last_seen_at
                    )
                    VALUES(?,?,?,?,1,CURRENT_TIMESTAMP)
                    ON CONFLICT(package_id, remote_relpath) DO UPDATE SET
                        media_id=excluded.media_id,
                        remote_etag=excluded.remote_etag,
                        active=1,
                        last_seen_at=CURRENT_TIMESTAMP
                    """,
                    (
                        location.package_id,
                        location.media_id,
                        location.remote_relpath,
                        location.remote_etag,
                    ),
                )

    async def deactivate_packages_not_seen(self, package_ids: set[str]) -> int:
        async with self._write_transaction() as conn:
            rows = conn.execute(
                "SELECT package_id FROM catalog_packages WHERE active=1"
            ).fetchall()
            missing = [
                str(row["package_id"])
                for row in rows
                if str(row["package_id"]) not in package_ids
            ]
            for package_id in missing:
                conn.execute(
                    "UPDATE catalog_packages SET active=0 WHERE package_id=?",
                    (package_id,),
                )
                conn.execute(
                    "UPDATE media_locations SET active=0 WHERE package_id=?",
                    (package_id,),
                )
            return len(missing)

    async def refresh_media_activity(self) -> None:
        async with self._write_transaction() as conn:
            conn.execute(
                """
                UPDATE media
                SET active = CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM media_locations ml
                        JOIN catalog_packages cp ON cp.package_id = ml.package_id
                        WHERE ml.media_id = media.media_id
                          AND ml.active = 1
                          AND cp.active = 1
                    ) THEN 1 ELSE 0 END
                """
            )

    async def count_active_videos(self) -> int:
        row = self._require().execute(
            "SELECT COUNT(*) AS count FROM media WHERE active=1 AND kind='video'"
        ).fetchone()
        return int(row["count"])

    async def list_active_video_ids(self, *, limit: int = 1000) -> list[str]:
        rows = self._require().execute(
            """
            SELECT media_id
            FROM media
            WHERE active=1 AND kind='video'
            ORDER BY media_id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [str(row["media_id"]) for row in rows]

    async def active_locations(self, media_id: str) -> list[tuple[str, str]]:
        rows = self._require().execute(
            """
            SELECT cp.remote_path, ml.remote_relpath
            FROM media_locations ml
            JOIN catalog_packages cp ON cp.package_id=ml.package_id
            WHERE ml.media_id=? AND ml.active=1 AND cp.active=1
            ORDER BY cp.package_id, ml.remote_relpath
            """,
            (media_id,),
        ).fetchall()
        return [
            (str(row["remote_path"]), str(row["remote_relpath"]))
            for row in rows
        ]
