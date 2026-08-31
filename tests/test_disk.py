import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.services.disk import DiskManager
from src.services.job_queue import JobQueue


class _Usage:
    def __init__(self, total: int, used: int, free: int) -> None:
        self.total = total
        self.used = used
        self.free = free


class DiskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "downloads"
        self.root.mkdir()

    def test_monitor_mode_reserves_but_does_not_reject_low_headroom(self) -> None:
        manager = DiskManager(
            self.root,
            enforce=False,
            min_free_bytes=500,
            min_free_percent=20,
            unknown_reserve_bytes=200,
        )
        with patch("src.services.disk.shutil.disk_usage", return_value=_Usage(1000, 700, 300)):
            decision = manager.reserve(1, None)
        self.assertFalse(decision.healthy)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.requested_bytes, 200)
        self.assertEqual(manager.reserved_bytes(1), 200)
        self.assertEqual(manager.release(1), 200)
        self.assertEqual(manager.reserved_bytes(), 0)

    def test_enforce_mode_accounts_for_concurrent_reservations(self) -> None:
        manager = DiskManager(
            self.root,
            enforce=True,
            min_free_bytes=200,
            min_free_percent=0,
        )
        with patch("src.services.disk.shutil.disk_usage", return_value=_Usage(1000, 400, 600)):
            first = manager.reserve(1, 200)
            second = manager.reserve(2, 250)
        self.assertTrue(first.allowed)
        self.assertFalse(second.allowed)
        self.assertEqual(manager.reserved_bytes(), 450)

    def test_managed_paths_cannot_escape_download_root(self) -> None:
        manager = DiskManager(self.root)
        job_dir = self.root / "job-123"
        job_dir.mkdir()
        self.assertEqual(manager.validate_job_dir(job_dir), job_dir.resolve())
        with self.assertRaises(ValueError):
            manager.validate_job_dir(self.root / "other")
        with self.assertRaises(ValueError):
            manager.validate_managed_path(self.root.parent / "outside")

    def test_cleanup_plan_is_oldest_first_and_fail_closed_on_protected_cache(self) -> None:
        manager = DiskManager(self.root)
        now = 1_000_000.0

        old_success = self.root / "job-1"
        old_success.mkdir()
        (old_success / "video.mp4").write_bytes(b"a" * 10)
        old_failed = self.root / "job-2"
        old_failed.mkdir()
        (old_failed / "video.mp4").write_bytes(b"b" * 20)
        active = self.root / "job-3"
        active.mkdir()
        (active / "video.mp4.part").write_bytes(b"c" * 30)
        webdav_failed = self.root / "job-4"
        webdav_failed.mkdir()
        (webdav_failed / "video.mp4").write_bytes(b"d" * 40)

        inventory = [
            {"job_id": 1, "legacy_seq": 11, "state": "succeeded", "local_dir": str(old_success), "finished_at": now - 100 * 3600},
            {"job_id": 2, "legacy_seq": 12, "state": "failed", "local_dir": str(old_failed), "finished_at": now - 200 * 3600},
            {"job_id": 3, "legacy_seq": 13, "state": "succeeded", "local_dir": str(active), "finished_at": now - 200 * 3600},
            {"job_id": 4, "legacy_seq": 14, "state": "failed", "local_dir": str(webdav_failed), "finished_at": now - 300 * 3600, "backup_state": "failed"},
        ]
        plan = manager.cleanup_plan(
            inventory,
            now=now,
            cache_retention_hours=72,
            failed_retention_hours=168,
        )

        self.assertEqual([item.job_id for item in plan.candidates], [2, 1])
        self.assertEqual(plan.reclaimable_bytes, 30)
        reasons = {item.job_id: item.reason for item in plan.protected}
        self.assertEqual(reasons[3], "partial-file")
        self.assertEqual(reasons[4], "backup-protected")
        self.assertEqual(plan.protected_bytes, 70)

    def test_cleanup_plan_rejects_retry_claim_and_unsafe_paths(self) -> None:
        manager = DiskManager(self.root)
        now = 2_000_000.0
        claimed = self.root / "job-5"
        claimed.mkdir()
        retrying = self.root / "job-6"
        retrying.mkdir()
        inventory = [
            {"job_id": 5, "state": "succeeded", "local_dir": str(claimed), "finished_at": 1.0, "claim_owner": "worker"},
            {"job_id": 6, "state": "failed", "local_dir": str(retrying), "finished_at": 1.0, "next_retry_at": now + 100},
            {"job_id": 7, "state": "succeeded", "local_dir": str(self.root.parent / "job-7"), "finished_at": 1.0},
        ]
        plan = manager.cleanup_plan(inventory, now=now, cache_retention_hours=0, failed_retention_hours=0)
        self.assertFalse(plan.candidates)
        reasons = {item.job_id: item.reason for item in plan.protected}
        self.assertEqual(reasons[5], "active-claim")
        self.assertEqual(reasons[6], "job-retry-window")
        self.assertEqual(reasons[7], "unsafe-path")


class CleanupDryRunIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_retry_and_webdav_sets_protect_durable_candidates(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name) / "downloads"
        root.mkdir()
        first = root / "job-10"
        second = root / "job-11"
        first.mkdir()
        second.mkdir()
        (first / "a.mp4").write_bytes(b"a")
        (second / "b.mp4").write_bytes(b"b")
        rows = [
            {"job_id": 10, "legacy_seq": 100, "state": "failed", "local_dir": str(first), "finished_at": 1.0},
            {"job_id": 11, "legacy_seq": 101, "state": "succeeded", "local_dir": str(second), "finished_at": 1.0},
        ]

        class Repo:
            async def cleanup_inventory(self):
                return rows

        pipeline = SimpleNamespace(
            repository=Repo(),
            disk=DiskManager(root),
            retryable={100: object()},
            webdav_keep_cache={101},
            _disk_cache_retention_hours=0.0,
            _disk_failed_retention_hours=0.0,
        )
        queue = JobQueue(pipeline, shadow=SimpleNamespace(job_ids={100: 10, 101: 11}))
        plan = await queue.disk_cleanup_plan()
        self.assertFalse(plan.candidates)
        reasons = {item.job_id: item.reason for item in plan.protected}
        self.assertEqual(reasons[10], "runtime-retry-protected")
        self.assertEqual(reasons[11], "runtime-webdav-protected")
