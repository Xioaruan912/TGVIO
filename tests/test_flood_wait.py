from __future__ import annotations

import unittest

from tgvio.application.flood_wait import (
    DEFAULT_WAIT_SECONDS,
    MAX_WAIT_SECONDS,
    FloodWaitGate,
    parse_flood_wait_seconds,
)
from tgvio.application.media_downloader import classify_download_error
from tgvio.domain.control import QueueControlState


class FloodWaitError(Exception):
    """Stands in for Telethon's error: the gate keys off the type name."""

    def __init__(self, seconds: int | None = None, *, value: int | None = None) -> None:
        super().__init__(f"A wait of {seconds} seconds is required")
        if seconds is not None:
            self.seconds = seconds
        if value is not None:
            self.value = value


class FakeRepository:
    """Minimal durable queue-control stub."""

    def __init__(self) -> None:
        self.state = QueueControlState(paused=False, pause_reason=None, revision=0)

    async def get_queue_control(self) -> QueueControlState:
        return self.state

    async def set_queue_paused(self, paused: bool, *, reason: str | None = None) -> QueueControlState:
        self.state = QueueControlState(
            paused=paused,
            pause_reason=reason if paused else None,
            revision=self.state.revision + 1,
        )
        return self.state


class ParseFloodWaitTests(unittest.TestCase):
    def test_typed_error_seconds_are_used(self) -> None:
        self.assertEqual(parse_flood_wait_seconds(FloodWaitError(42)), 42)
        self.assertEqual(parse_flood_wait_seconds(FloodWaitError(value=7)), 7)

    def test_wrapped_errors_are_recognised_from_text(self) -> None:
        self.assertEqual(parse_flood_wait_seconds(RuntimeError("A wait of 21 seconds is required")), 21)
        self.assertEqual(parse_flood_wait_seconds(RuntimeError("Telegram says FLOOD_WAIT_90")), 90)

    def test_marker_without_a_number_uses_the_default(self) -> None:
        self.assertEqual(parse_flood_wait_seconds(FloodWaitError()), DEFAULT_WAIT_SECONDS)
        self.assertEqual(parse_flood_wait_seconds(RuntimeError("FLOOD_WAIT")), DEFAULT_WAIT_SECONDS)

    def test_absurd_waits_are_clamped_and_other_errors_ignored(self) -> None:
        self.assertEqual(parse_flood_wait_seconds(FloodWaitError(10 ** 9)), MAX_WAIT_SECONDS)
        self.assertIsNone(parse_flood_wait_seconds(RuntimeError("connection reset")))
        self.assertIsNone(parse_flood_wait_seconds(TimeoutError("Timeout while fetching data")))


class FloodWaitGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repository = FakeRepository()
        self.now = 1000.0
        self.gate = FloodWaitGate(self.repository, clock=lambda: self.now)

    async def test_arm_pauses_the_queue_and_tick_resumes_after_the_window(self) -> None:
        until = await self.gate.arm(FloodWaitError(60))
        self.assertEqual(until, 1060)
        self.assertTrue(self.repository.state.paused)
        self.assertEqual(self.repository.state.pause_reason, "flood_wait:1060")

        self.now = 1059.0
        self.assertEqual(await self.gate.tick(), 1060)
        self.assertTrue(self.repository.state.paused, "the gate must hold until the window expires")

        self.now = 1060.0
        self.assertIsNone(await self.gate.tick())
        self.assertFalse(self.repository.state.paused)
        self.assertIsNone(self.repository.state.pause_reason)

    async def test_repeat_waits_never_shorten_the_window(self) -> None:
        self.assertEqual(await self.gate.arm(FloodWaitError(120)), 1120)
        # A later, shorter instruction must not cut an existing longer wait.
        self.assertEqual(await self.gate.arm(FloodWaitError(30)), 1120)
        self.assertEqual(self.repository.state.pause_reason, "flood_wait:1120")
        # A longer one extends it.
        self.assertEqual(await self.gate.arm(FloodWaitError(600)), 1600)
        self.assertEqual(self.repository.state.pause_reason, "flood_wait:1600")

    async def test_a_manual_pause_is_left_alone(self) -> None:
        await self.repository.set_queue_paused(True, reason="owner-maintenance")
        self.now = 99_999.0
        self.assertIsNone(await self.gate.tick())
        self.assertTrue(self.repository.state.paused)
        self.assertEqual(self.repository.state.pause_reason, "owner-maintenance")

    async def test_unrelated_errors_do_not_pause_publishing(self) -> None:
        self.assertIsNone(await self.gate.arm(RuntimeError("disk full")))
        self.assertFalse(self.repository.state.paused)
        self.assertIsNone(await self.gate.tick())


class DownloadFloodWaitClassificationTests(unittest.TestCase):
    def test_a_flood_wait_is_reported_as_its_own_failure_code(self) -> None:
        code, message = classify_download_error(FloodWaitError(45))
        self.assertEqual(code, "telegram_flood_wait")
        self.assertIn("限流", message)

    def test_timeouts_keep_their_existing_classification(self) -> None:
        code, _ = classify_download_error(TimeoutError("Timeout while fetching data"))
        self.assertEqual(code, "telegram_file_timeout")


if __name__ == "__main__":
    unittest.main()
