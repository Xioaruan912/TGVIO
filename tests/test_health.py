import asyncio
import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.health import RuntimeHeartbeat


def _load_healthcheck_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "healthcheck.py"
    spec = importlib.util.spec_from_file_location("healthcheck_script", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RuntimeHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_tracks_ready_state_and_stops_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime-health.json"
            heartbeat = RuntimeHeartbeat(path, interval=0.01)
            heartbeat.start()
            await asyncio.sleep(0.02)
            first = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(first["ready"])
            self.assertEqual(first["pid"], os.getpid())
            heartbeat.set_ready(True)
            ready = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(ready["ready"])
            await heartbeat.stop()
            stopped = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(stopped["ready"])


class LocalHealthcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.healthcheck = _load_healthcheck_module()

    def _fixture(self, root: Path, *, ready: bool, heartbeat_at: float | None = None):
        heartbeat = root / "runtime-health.json"
        heartbeat.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pid": os.getpid(),
                    "heartbeat_at": time.time() if heartbeat_at is None else heartbeat_at,
                    "ready": ready,
                }
            ),
            encoding="utf-8",
        )
        db = root / "state.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE smoke(id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        downloads = root / "downloads"
        downloads.mkdir()
        return heartbeat, db, downloads

    def test_liveness_ignores_readiness_and_external_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            heartbeat, db, downloads = self._fixture(root, ready=False)
            env = {
                "HEALTH_HEARTBEAT_FILE": str(heartbeat),
                "STATE_DB": str(db),
                "DOWNLOAD_DIR": str(downloads),
                "HEALTH_MIN_FREE_BYTES": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                live, live_reason = self.healthcheck.check(require_ready=False)
                ready, ready_reason = self.healthcheck.check(require_ready=True)
            self.assertTrue(live, live_reason)
            self.assertFalse(ready)
            self.assertEqual(ready_reason, "not_ready")

    def test_stale_heartbeat_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            heartbeat, db, downloads = self._fixture(
                root,
                ready=True,
                heartbeat_at=time.time() - 120,
            )
            env = {
                "HEALTH_HEARTBEAT_FILE": str(heartbeat),
                "STATE_DB": str(db),
                "DOWNLOAD_DIR": str(downloads),
                "HEALTH_HEARTBEAT_MAX_AGE": "45",
                "HEALTH_MIN_FREE_BYTES": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                ok, reason = self.healthcheck.check(require_ready=False)
            self.assertFalse(ok)
            self.assertEqual(reason, "heartbeat_stale")

    def test_dockerfile_healthcheck_does_not_reference_external_probes(self) -> None:
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('HEALTHCHECK', dockerfile)
        self.assertIn('scripts/healthcheck.py', dockerfile)
        self.assertNotIn('curl', dockerfile.lower())
