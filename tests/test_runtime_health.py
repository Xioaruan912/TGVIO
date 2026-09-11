from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.runtime_health import RuntimeHealthHeartbeat
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class RuntimeHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_persists_runtime_and_telegram_state(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = SQLiteJobRepository(Path(tmp) / "state.sqlite3")
            await repo.open()
            try:
                heartbeat = RuntimeHealthHeartbeat(
                    repo,
                    lambda: True,
                    interval_seconds=60,
                )
                await heartbeat.start()
                health = await repo.get_runtime_health()
                self.assertEqual(health["runtime"]["status"], "alive")
                self.assertEqual(health["telegram"]["status"], "connected")
                await heartbeat.stop()
            finally:
                await repo.close()

    async def test_healthcheck_rejects_disconnected_or_stale_runtime(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            repo = SQLiteJobRepository(db)
            await repo.open()
            try:
                await repo.set_runtime_health("runtime", "alive")
                await repo.set_runtime_health("telegram", "connected")
            finally:
                await repo.close()

            spec = importlib.util.spec_from_file_location(
                "tgvio_healthcheck",
                Path("scripts/healthcheck.py"),
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertTrue(module.check_health(db, require_runtime=True))

            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "UPDATE runtime_health SET status='disconnected' WHERE component='telegram'"
                )
                conn.commit()
            finally:
                conn.close()
            self.assertFalse(module.check_health(db, require_runtime=True))

            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "UPDATE runtime_health SET status='connected', updated_at='2000-01-01 00:00:00' WHERE component='telegram'"
                )
                conn.commit()
            finally:
                conn.close()
            self.assertFalse(module.check_health(db, require_runtime=True))

    async def test_healthcheck_without_runtime_requirement_only_checks_database(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            repo = SQLiteJobRepository(db)
            await repo.open()
            await repo.close()
            spec = importlib.util.spec_from_file_location(
                "tgvio_healthcheck_no_runtime",
                Path("scripts/healthcheck.py"),
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertTrue(module.check_health(db, require_runtime=False))
