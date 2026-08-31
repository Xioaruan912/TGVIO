import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.disk import DiskManager


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
