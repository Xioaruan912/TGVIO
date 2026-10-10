from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_rendition_sidecars import fixture
from tgvio.infrastructure.rendition_state import MaintenanceState
from tgvio.infrastructure.cover_frames import extract


class CompletionTests(unittest.TestCase):
    def test_persistent_watch_retries_after_fast_budget_without_hot_loop(self):
        _, task = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            state = MaintenanceState(Path(tmp)/"state.sqlite3", persistent_retry=True)
            state.discover([task])
            with patch("tgvio.infrastructure.rendition_state.time.time", return_value=1000):
                for _ in range(5):
                    state.fail(task, TimeoutError())
            self.assertFalse(state.eligible(task, now=4599))
            self.assertTrue(state.eligible(task, now=4600))
            self.assertEqual(state.summary()["blocked"], 0)
            self.assertEqual(state.summary()["slow_retry"], 1)
            with patch("tgvio.infrastructure.rendition_state.time.time", return_value=1000):
                for _ in range(10):
                    state.fail(task, TimeoutError())
            self.assertFalse(state.eligible(task, now=22599))
            self.assertTrue(state.eligible(task, now=22600))
            state.conn.close()
            state = MaintenanceState(Path(tmp)/"state.sqlite3", persistent_retry=True)
            self.assertTrue(state.eligible(task, now=22600))
            self.assertEqual(state.summary()["failed"], 1)
            state.conn.close()

    def test_due_failures_receive_slots_without_starving_new_tasks(self):
        _, task = fixture()
        tasks = [replace(task, package_id=f"package-{i}") for i in range(20)]
        with tempfile.TemporaryDirectory() as tmp:
            state = MaintenanceState(Path(tmp)/"state.sqlite3", persistent_retry=True)
            state.discover(tasks)
            with patch("tgvio.infrastructure.rendition_state.time.time", return_value=100):
                for item in tasks[10:]:
                    state.fail(item, TimeoutError())
            batch = state.ready_batch(tasks, limit=10, now=1000)
            self.assertEqual(batch[:3], tasks[10:13])
            self.assertEqual(batch[3:], tasks[:7])
            self.assertEqual(len({t.key for t in batch}), 10)
            self.assertEqual(state.ready_batch(tasks, limit=10, now=699), tasks[:10])
            state.conn.close()


class RecheckOrderTests(unittest.TestCase):
    def test_new_work_comes_before_the_daily_recheck(self):
        _, task = fixture()
        tasks = [replace(task, package_id=f"package-{i}") for i in range(20)]
        with tempfile.TemporaryDirectory() as tmp:
            state = MaintenanceState(Path(tmp)/"state.sqlite3", persistent_retry=True)
            state.discover(tasks)
            with patch("tgvio.infrastructure.rendition_state.time.time", return_value=0):
                for item in tasks[:15]:
                    state.finish(item, 1)
            # A day later all 15 finished tasks are due for a re-check, yet the
            # five never-done ones lead the batch.
            batch = state.ready_batch(tasks, limit=10, now=90_000)
            self.assertEqual(batch[:5], tasks[15:])
            self.assertEqual(batch[5:], tasks[:5])
            state.conn.close()


class GoneSourceTests(unittest.TestCase):
    def test_a_gone_source_is_neither_done_nor_retried_as_a_failure(self):
        _, task = fixture()
        tasks = [replace(task, package_id=f"package-{i}") for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            state = MaintenanceState(Path(tmp)/"state.sqlite3", persistent_retry=True)
            state.discover(tasks)
            with patch("tgvio.infrastructure.rendition_state.time.time", return_value=0):
                state.gone(tasks[0])
            summary = state.summary()
            self.assertEqual((summary["gone"], summary["failed"], summary["done"]), (1, 0, 0))
            self.assertNotIn(tasks[0], state.ready_batch(tasks, limit=10, now=3600))
            # A day later it is looked at again, after the work never done.
            self.assertEqual(state.ready_batch(tasks, limit=10, now=90_000), [tasks[1], tasks[2], tasks[0]])
            state.conn.close()


class FrameTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_frame_is_cancelled_then_another_real_candidate_is_tried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            positions = []
            image = bytes([255,216])+b"fixture"+bytes([255,217])
            async def command(*args):
                if "rawvideo" in args:
                    return 0, bytes([128])*1024
                position = args[args.index("-ss")+1]
                positions.append(position)
                if position == "1":
                    raise TimeoutError()
                (root/"frame.jpg").write_bytes(image)
                return 0, b""
            with patch("tgvio.infrastructure.cover_frames.command", side_effect=command):
                self.assertEqual((await extract(root/"source.mp4", root)).payload, image)
            self.assertEqual(positions, ["1", "0.1"])

    async def test_cancellation_never_starts_another_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("tgvio.infrastructure.cover_frames.command",
                       side_effect=asyncio.CancelledError()) as command:
                with self.assertRaises(asyncio.CancelledError):
                    await extract(root/"source.mp4", root)
            self.assertEqual(command.call_count, 1)


class QueueLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cover_batches_reuse_discovery_and_keep_processing_fresh_work(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from tgvio.interfaces.backfill_covers import work
        _, task = fixture()
        tasks = [replace(task, package_id=f"package-{i}") for i in range(30)]
        discovery = SimpleNamespace(tasks=AsyncMock(return_value=tasks))
        runner = SimpleNamespace(run=AsyncMock(return_value=1))
        batches = 0
        async def sleep(seconds):
            nonlocal batches
            if seconds == 10:
                batches += 1
                if batches == 2:
                    raise asyncio.CancelledError()
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(work_dir=tmp, status=False, retry_failed=False,
                                   priority_file=None, watch=True, dry_run=False, limit=0)
            with patch.dict("os.environ", {
                "TGVIO_ARCHIVE_WEBDAV_URL":"https://fixture.invalid",
                "TGVIO_ARCHIVE_WEBDAV_USER":"fixture",
                "TGVIO_ARCHIVE_WEBDAV_PASSWORD":"fixture"}), \
                 patch("tgvio.interfaces.backfill_covers.CoverArchivePort"), \
                 patch("tgvio.interfaces.backfill_covers.CoverDiscovery", return_value=discovery), \
                 patch("tgvio.interfaces.backfill_covers.CommittedCoverBackfill", return_value=runner), \
                 patch("tgvio.interfaces.backfill_covers.report"), \
                 patch("tgvio.interfaces.backfill_covers.asyncio.sleep", side_effect=sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await work(args)
            self.assertEqual(discovery.tasks.await_count, 1)
            self.assertEqual(runner.run.await_count, 20)
            keys = [call.args[0].key for call in runner.run.await_args_list]
            self.assertEqual(len(set(keys)), 20)
            state = MaintenanceState(Path(tmp)/"progress.sqlite3", persistent_retry=True)
            self.assertEqual(state.summary()["done"], 20)
            state.conn.close()

    async def test_rendition_watch_recovers_scan_failure_without_process_restart(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from tgvio.interfaces.backfill_renditions import work
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(work_dir=tmp, status=False, retry_failed=False,
                                   watch=True, dry_run=True, limit=0)
            discovery = Mock(tasks=AsyncMock(side_effect=[TimeoutError(), []]))
            with patch.dict("os.environ", {
                "TGVIO_ARCHIVE_WEBDAV_URL":"https://fixture.invalid",
                "TGVIO_ARCHIVE_WEBDAV_USER":"fixture",
                "TGVIO_ARCHIVE_WEBDAV_PASSWORD":"fixture"}), \
                 patch("tgvio.interfaces.backfill_renditions.RenditionArchivePort"), \
                 patch("tgvio.interfaces.backfill_renditions.RenditionDiscovery", return_value=discovery), \
                 patch("tgvio.interfaces.backfill_renditions.asyncio.sleep", new=AsyncMock()), \
                 patch("tgvio.interfaces.backfill_renditions.report") as report:
                await work(args)
            self.assertEqual(discovery.tasks.await_count, 2)
            self.assertEqual([c.args[0] for c in report.call_args_list],
                             ["scan_failed", "scan", "plan"])
