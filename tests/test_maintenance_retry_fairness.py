from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_rendition_sidecars import fixture
from tgvio.infrastructure.rendition_state import MaintenanceState


class RetryFairnessTests(unittest.TestCase):
    def test_batches_longer_than_cooldown_do_not_repeat_the_same_failed_prefix(self):
        _, task = fixture()
        tasks = [replace(task, package_id=f"package-{i}") for i in range(26)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            state = MaintenanceState(path, persistent_retry=True)
            state.discover(tasks)
            with patch("tgvio.infrastructure.rendition_state.time.time", return_value=100):
                for item in tasks[:6]:
                    state.fail(item, TimeoutError())
            batch = state.ready_batch(tasks, limit=10, now=1000)
            self.assertEqual(batch[:3], tasks[:3])
            self.assertEqual(batch[3:], tasks[6:13])
            # A slow encode batch takes longer than the failure cooldown.
            with patch("tgvio.infrastructure.rendition_state.time.time", return_value=1100):
                for item in batch[:3]:
                    state.fail(item, TimeoutError())
                for item in batch[3:]:
                    state.finish(item, 2)
            state.conn.close()
            state = MaintenanceState(path, persistent_retry=True)
            before = list(state.conn.execute("SELECT * FROM tasks ORDER BY key"))
            self.assertTrue(all(state.eligible(t, now=2100) for t in tasks[:6]))
            next_batch = state.ready_batch(tasks, limit=10, now=2100)
            self.assertEqual(next_batch[:3], tasks[3:6],
                             "older due failures must get their turn despite input order")
            self.assertEqual(next_batch[3:], tasks[13:20])
            self.assertEqual(before, list(state.conn.execute("SELECT * FROM tasks ORDER BY key")))
            self.assertEqual(len({t.key for t in next_batch}), 10)
            state.conn.close()

    def test_spare_retry_slots_also_choose_the_oldest_due_attempts(self):
        _, task = fixture()
        tasks = [replace(task, package_id=f"package-{i}") for i in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            state = MaintenanceState(Path(tmp) / "state.sqlite3", persistent_retry=True)
            state.discover(tasks)
            for i, item in enumerate(tasks):
                with patch("tgvio.infrastructure.rendition_state.time.time", return_value=100-i):
                    state.fail(item, TimeoutError())
            expected = list(reversed(tasks))
            self.assertEqual(state.ready_batch(tasks, limit=6, now=1000), expected[:6])
            self.assertEqual(state.ready_batch(tasks, now=1000), expected)
            state.conn.close()
