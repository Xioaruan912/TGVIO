from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
import time
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

    async def list_video_ids(
        self,
        *,
        min_seconds: float | None = None,
        max_seconds: float | None = None,
        order: str = "media_id",
        limit: int = 1000,
        offset: int = 0,
    ) -> list[str]:
        clauses = ["active=1", "kind='video'"]
        params: list[object] = []
        if min_seconds is not None:
            clauses.append("duration_seconds > ?")
            params.append(float(min_seconds))
        if max_seconds is not None:
            clauses.append("duration_seconds <= ?")
            params.append(float(max_seconds))
        order_sql = {
            "duration_desc": "duration_seconds DESC, media_id",
            "duration_asc": "duration_seconds ASC, media_id",
        }.get(order, "media_id")
        params.extend([max(1, int(limit)), max(0, int(offset))])
        rows = self._require().execute(
            f"SELECT media_id FROM media WHERE {' AND '.join(clauses)} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            tuple(params),
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

    async def active_media_location(
        self, media_id: str
    ) -> tuple[str, str, str | None] | None:
        """Return one catalog-owned active location, never a caller-supplied path."""
        row = self._require().execute(
            """
            SELECT cp.remote_path, ml.remote_relpath, ml.remote_etag
            FROM media_locations ml
            JOIN catalog_packages cp ON cp.package_id=ml.package_id
            JOIN media ON media.media_id=ml.media_id
            WHERE ml.media_id=? AND ml.active=1 AND cp.active=1 AND media.active=1
            ORDER BY cp.package_id, ml.remote_relpath
            LIMIT 1
            """,
            (media_id,),
        ).fetchone()
        if row is None:
            return None
        return (
            str(row["remote_path"]),
            str(row["remote_relpath"]),
            str(row["remote_etag"]) if row["remote_etag"] is not None else None,
        )

    async def active_media_details(self, media_id: str) -> dict[str, object] | None:
        row = self._require().execute(
            """
            SELECT media_id, kind, mime_type, size_bytes, width, height,
                   duration_seconds, container, codec
            FROM media WHERE media_id=? AND active=1
            """,
            (media_id,),
        ).fetchone()
        return None if row is None else dict(row)

    async def create_player_session(self, token_digest: str, *, expires_at: int) -> None:
        now = int(time.time())
        async with self._write_transaction() as conn:
            conn.execute("DELETE FROM player_sessions WHERE expires_at<=?", (now,))
            conn.execute(
                "INSERT INTO player_sessions(token_digest, created_at, expires_at) VALUES(?,?,?)",
                (token_digest, now, expires_at),
            )
            conn.execute(
                "INSERT INTO feed_sessions(token_digest, last_active_at) VALUES(?,?)",
                (token_digest, now),
            )

    async def has_player_session(self, token_digest: str, *, now: int) -> bool:
        row = self._require().execute(
            "SELECT 1 FROM player_sessions WHERE token_digest=? AND expires_at>?",
            (token_digest, now),
        ).fetchone()
        return row is not None

    async def delete_player_session(self, token_digest: str) -> None:
        async with self._write_transaction() as conn:
            conn.execute("DELETE FROM player_sessions WHERE token_digest=?", (token_digest,))

    async def has_unconsumed_feed_items(self, token_digest: str) -> bool:
        row = self._require().execute(
            """
            SELECT 1 FROM feed_session_items
            WHERE token_digest=? AND consumed_at IS NULL LIMIT 1
            """,
            (token_digest,),
        ).fetchone()
        return row is not None

    async def next_feed_cycle(self, token_digest: str) -> int:
        row = self._require().execute(
            "SELECT current_cycle FROM feed_sessions WHERE token_digest=?", (token_digest,)
        ).fetchone()
        if row is None:
            raise RuntimeError("unknown player session")
        return int(row["current_cycle"]) + 1

    async def recent_feed_media(self, token_digest: str, *, limit: int) -> list[str]:
        if limit < 1:
            return []
        rows = self._require().execute(
            """
            SELECT media_id FROM feed_recent_media
            WHERE token_digest=? ORDER BY id DESC LIMIT ?
            """,
            (token_digest, limit),
        ).fetchall()
        return [str(row["media_id"]) for row in rows]

    async def write_feed_cycle(
        self, token_digest: str, *, cycle: int, media_ids: list[str]
    ) -> None:
        now = int(time.time())
        async with self._write_transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM player_sessions WHERE token_digest=?", (token_digest,)
            ).fetchone()
            if exists is None:
                raise RuntimeError("unknown player session")
            conn.execute(
                "UPDATE feed_sessions SET current_cycle=?, last_active_at=? WHERE token_digest=?",
                (cycle, now, token_digest),
            )
            conn.executemany(
                """
                INSERT INTO feed_session_items(token_digest, cycle, ordinal, media_id)
                VALUES(?,?,?,?)
                """,
                [(token_digest, cycle, ordinal, media_id) for ordinal, media_id in enumerate(media_ids)],
            )

    async def consume_feed_items(self, token_digest: str, *, limit: int) -> list[str]:
        if limit < 1:
            return []
        now = int(time.time())
        async with self._write_transaction() as conn:
            # Recheck current catalog activity at consumption time: a package can
            # disappear after a cycle was created.
            rows = conn.execute(
                """
                SELECT item.cycle, item.ordinal, item.media_id
                FROM feed_session_items item
                JOIN media ON media.media_id=item.media_id
                WHERE item.token_digest=? AND item.consumed_at IS NULL AND media.active=1
                ORDER BY item.cycle, item.ordinal LIMIT ?
                """,
                (token_digest, limit),
            ).fetchall()
            result = [str(row["media_id"]) for row in rows]
            for row in rows:
                conn.execute(
                    """
                    UPDATE feed_session_items SET consumed_at=?
                    WHERE token_digest=? AND cycle=? AND ordinal=? AND consumed_at IS NULL
                    """,
                    (now, token_digest, row["cycle"], row["ordinal"]),
                )
                conn.execute(
                    "INSERT INTO feed_recent_media(token_digest, media_id, consumed_at) VALUES(?,?,?)",
                    (token_digest, row["media_id"], now),
                )
            # Inactive deck entries are terminally skipped so a later cycle can
            # start once all remaining active entries have been consumed.
            conn.execute(
                """
                UPDATE feed_session_items SET consumed_at=?
                WHERE token_digest=? AND consumed_at IS NULL
                  AND media_id IN (SELECT media_id FROM media WHERE active=0)
                """,
                (now, token_digest),
            )
            conn.execute(
                "UPDATE feed_sessions SET last_active_at=? WHERE token_digest=?",
                (now, token_digest),
            )
            return result

    async def set_favorite(self, token_digest: str, media_id: str, *, enabled: bool) -> None:
        now = int(time.time())
        async with self._write_transaction() as conn:
            if enabled:
                conn.execute(
                    """
                    INSERT INTO favorites(token_digest, media_id, created_at) VALUES(?,?,?)
                    ON CONFLICT(token_digest, media_id) DO NOTHING
                    """,
                    (token_digest, media_id, now),
                )
            else:
                conn.execute(
                    "DELETE FROM favorites WHERE token_digest=? AND media_id=?",
                    (token_digest, media_id),
                )

    async def is_favorite(self, token_digest: str, media_id: str) -> bool:
        row = self._require().execute(
            "SELECT 1 FROM favorites WHERE token_digest=? AND media_id=?",
            (token_digest, media_id),
        ).fetchone()
        return row is not None

    async def list_favorite_ids(self, token_digest: str, *, limit: int = 200) -> list[str]:
        rows = self._require().execute(
            """
            SELECT favorites.media_id
            FROM favorites
            JOIN media ON media.media_id=favorites.media_id
            WHERE favorites.token_digest=? AND media.active=1 AND media.kind='video'
            ORDER BY favorites.created_at DESC, favorites.media_id
            LIMIT ?
            """,
            (token_digest, max(1, int(limit))),
        ).fetchall()
        return [str(row["media_id"]) for row in rows]
