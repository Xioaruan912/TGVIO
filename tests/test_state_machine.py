import unittest

from src.state_machine import InvalidTransition, plan_transition


class JobStateMachineTests(unittest.TestCase):
    def test_pause_records_resume_state_and_only_resumes_there(self) -> None:
        paused = plan_transition("downloading", "paused")
        self.assertEqual(paused.resume_state, "downloading")
        resumed = plan_transition(
            "paused", "downloading", current_resume_state=paused.resume_state
        )
        self.assertIsNone(resumed.resume_state)
        with self.assertRaises(InvalidTransition):
            plan_transition("paused", "ready", current_resume_state="downloading")

    def test_failed_retry_paths_are_explicit(self) -> None:
        self.assertEqual(plan_transition("failed", "queued").to_state, "queued")
        self.assertEqual(plan_transition("failed", "ready").to_state, "ready")

    def test_paused_job_can_be_cancelled(self) -> None:
        plan = plan_transition("paused", "cancelled", current_resume_state="ready")
        self.assertEqual(plan.to_state, "cancelled")

    def test_terminal_states_cannot_transition(self) -> None:
        for state in ("succeeded", "cancelled"):
            with self.subTest(state=state):
                with self.assertRaises(InvalidTransition):
                    plan_transition(state, "queued")

    def test_illegal_shortcuts_are_rejected(self) -> None:
        for source, target in (
            ("queued", "succeeded"),
            ("downloading", "publishing"),
            ("ready", "succeeded"),
        ):
            with self.subTest(source=source, target=target):
                with self.assertRaises(InvalidTransition):
                    plan_transition(source, target)
