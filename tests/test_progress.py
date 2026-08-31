import unittest

from src.progress import ProgressTracker


class ProgressTrackerTests(unittest.TestCase):
    def test_per_job_phase_throttle_and_speed_eta(self) -> None:
        tracker = ProgressTracker(ui_interval=2.0, edit_rate=100.0, edit_burst=100.0)
        first = tracker.update(1, "downloading", 100, 1000, 1, 1, now=10.0)
        second_job = tracker.update(2, "downloading", 200, 1000, 1, 1, now=10.0)
        self.assertTrue(tracker.allow_ui(first, now=10.0))
        self.assertTrue(tracker.allow_ui(second_job, now=10.0))
        self.assertFalse(tracker.allow_ui(first, now=11.0))
        second = tracker.update(1, "downloading", 300, 1000, 1, 1, now=12.0)
        self.assertIsNotNone(second.speed_bps)
        self.assertIsNotNone(second.eta_seconds)
        self.assertTrue(tracker.allow_ui(second, now=12.0))

    def test_db_gate_and_late_phase_are_independent(self) -> None:
        tracker = ProgressTracker(db_interval=5.0, db_bytes=32)
        state = tracker.update(7, "downloading", 10, 100, 1, 1, now=10.0)
        self.assertTrue(tracker.allow_db(state, now=10.0))
        state = tracker.update(7, "downloading", 20, 100, 1, 1, now=11.0)
        self.assertFalse(tracker.allow_db(state, now=11.0))
        state = tracker.update(7, "downloading", 60, 100, 1, 1, now=12.0)
        self.assertTrue(tracker.allow_db(state, now=12.0))
        tracker.begin_phase(7, "publishing")
        self.assertTrue(tracker.phase_is_stale(7, "downloading"))
        self.assertFalse(tracker.phase_is_stale(7, "publishing"))

    def test_flood_wait_defers_all_ui_edits(self) -> None:
        tracker = ProgressTracker(edit_rate=100.0, edit_burst=100.0)
        state = tracker.update(1, "downloading", 1, 10, 1, 1, now=5.0)
        tracker.defer_ui(10, now=5.0)
        self.assertFalse(tracker.allow_ui(state, now=9.0))
        self.assertTrue(tracker.allow_ui(state, now=15.0))

    def test_unknown_total_has_no_eta_and_global_edit_bucket_is_bounded(self) -> None:
        tracker = ProgressTracker(
            ui_interval=0.0,
            edit_rate=0.0,
            edit_burst=2.0,
        )
        unknown = tracker.update(1, "downloading", 100, 0, 1, 1, now=10.0)
        unknown = tracker.update(1, "downloading", 200, 0, 1, 1, now=11.0)
        self.assertEqual(unknown.pct, 0)
        self.assertIsNotNone(unknown.speed_bps)
        self.assertIsNone(unknown.eta_seconds)

        first = tracker.update(2, "downloading", 1, 10, 1, 1, now=11.0)
        second = tracker.update(3, "downloading", 1, 10, 1, 1, now=11.0)
        third = tracker.update(4, "downloading", 1, 10, 1, 1, now=11.0)
        self.assertTrue(tracker.allow_ui(first, now=11.0))
        self.assertTrue(tracker.allow_ui(second, now=11.0))
        self.assertFalse(tracker.allow_ui(third, now=11.0))


if __name__ == "__main__":
    unittest.main()
