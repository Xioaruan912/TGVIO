from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from tgvio.infrastructure.migration_runner import MigrationReport, MigrationRunner


class SQLiteRepositoryBase:
    """Shared SQLite connection, migration lifecycle, and write transaction boundary."""

    def __init__(
        self,
        path: Path,
        *,
        migrations_dir: Path | None = None,
        backup_dir: Path | None = None,
    ) -> None:
        self._path = path
        self._migrations_dir = migrations_dir
        self._backup_dir = backup_dir
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._migration_report: MigrationReport | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        runner = MigrationRunner(
            self._path,
            migrations_dir=self._migrations_dir,
            backup_dir=self._backup_dir,
        )
        self._migration_report = await asyncio.to_thread(runner.run)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=FULL")

    def schema_status(self) -> dict[str, object]:
        if self._migration_report is None:
            raise RuntimeError("repository is not open")
        return self._migration_report.safe_projection()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("repository is not open")
        return self._conn

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            conn = self._require()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                await conn.rollback()
                raise
            else:
                await conn.commit()
