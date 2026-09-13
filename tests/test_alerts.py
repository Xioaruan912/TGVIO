from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tgvio.adapters.telegram.alerts import TelegramOwnerNotifier, render_alert
from tgvio.application.alerts import AlertRuntime
from tgvio.application.notifications import NotificationRuntime, WebhookDeliveryError
from tgvio.domain.notifications import ALERT_EVENT_TYPES, NotificationEvent
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class _RecordingNotifier:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def deliver(self, payload: dict[str, object]) -> None:
        self.payloads.append(payload)


class AlertRenderTests(unittest.TestCase):
    def test_render_is_redacted_and_uses_human_fields(self) -> None:
        text = render_alert(
            {
                "event": "job.failed",
                "accepted_order": 12,
                "state": "failed",
                "error_code": "publish_partial",
                "job_id": "abcdef0123456789",
            }
        )
        self.assertIn("任务失败", text)
        self.assertIn("#12", text)
        self.assertIn("publish_partial", text)
        self.assertNotIn("abcdef", text)

    def test_render_disk_low_includes_bytes(self) -> None:
        text = render_alert(
            {
                "event": "runtime.disk_low",
                "component": "disk",
                "free_bytes": 1024,
                "reserve_bytes": 2048,
            }
        )
        self.assertIn("磁盘空间不足", text)
        self.assertIn("1.0 KiB", text)


class TelegramOwnerNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivers_and_wraps_failures(self) -> None:
        sent: list[str] = []

        async def sender(text: str):
            sent.append(text)

        notifier = TelegramOwnerNotifier(sender)
        await notifier.deliver({"event": "job.failed", "accepted_order": 1})
        self.assertEqual(len(sent), 1)

        async def broken(text: str):
            raise RuntimeError("telegram down")

        with self.assertRaises(WebhookDeliveryError) as ctx:
            await TelegramOwnerNotifier(broken).deliver({"event": "job.failed"})
        self.assertEqual(ctx.exception.code, "telegram_alert_failed")


class AlertRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.clock = 1_000_000.0
        self.connected = True
        self.free = 10_000

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    def _runtime(self, **overrides) -> AlertRuntime:
        values = {
            "telegram_connected": lambda: self.connected,
            "disk_free_bytes": lambda: self.free,
            "reserve_bytes": 0,
            "cooldown_seconds": 3600,
            "now": lambda: self.clock,
        }
        values.update(overrides)
        return AlertRuntime(self.repo, **values)

    async def test_telegram_disconnect_then_recovery(self) -> None:
        runtime = self._runtime()
        self.connected = False
        await runtime.run_once()
        counts = await self.repo.count_notification_outbox()
        self.assertEqual(counts["pending"], 1)
        # Same bucket: no duplicate.
        await runtime.run_once()
        self.assertEqual((await self.repo.count_notification_outbox())["pending"], 1)
        self.connected = True
        await runtime.run_once()
        self.assertEqual((await self.repo.count_notification_outbox())["pending"], 2)

    async def test_cooldown_reemits_after_window(self) -> None:
        runtime = self._runtime()
        self.connected = False
        await runtime.run_once()
        self.clock += 3600
        await runtime.run_once()
        self.assertEqual((await self.repo.count_notification_outbox())["pending"], 2)

    async def test_disk_low_and_recovery(self) -> None:
        runtime = self._runtime(reserve_bytes=5000)
        self.free = 100
        await runtime.run_once()
        self.assertEqual((await self.repo.count_notification_outbox())["pending"], 1)
        self.free = 9000
        await runtime.run_once()
        self.assertEqual((await self.repo.count_notification_outbox())["pending"], 2)


class NotificationPrimaryFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_only_receives_alert_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteJobRepository(Path(tmp) / "state.sqlite3")
            await repo.open()
            try:
                now = 500.0
                await repo.enqueue_notification(
                    NotificationEvent("job.succeeded", "job:1:succeeded", {"job_id": "1", "state": "succeeded"}),
                    now=now,
                )
                await repo.enqueue_notification(
                    NotificationEvent("job.failed", "job:2:failed", {"job_id": "2", "state": "failed"}),
                    now=now,
                )
                primary = _RecordingNotifier()
                runtime = NotificationRuntime(
                    repo,
                    primary,
                    primary_accepts=ALERT_EVENT_TYPES,
                    now=lambda: now,
                )
                await runtime.run_once()
                events = [payload.get("event") for payload in primary.payloads]
                self.assertEqual(events, ["job.failed"])
            finally:
                await repo.close()
