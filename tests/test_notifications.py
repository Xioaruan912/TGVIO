import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

from src.repository import RepositoryError, SQLiteRepository
from src.services.notifications import WebhookNotifier, WebhookResponse, WebhookTransportError


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Transport:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = []

    async def send(self, url, body, headers, timeout):
        self.calls.append((url, body, headers, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return WebhookResponse(int(response))


class NotificationRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        downloads = root / "downloads"
        downloads.mkdir()
        self.repo = SQLiteRepository(
            root / "state.sqlite3",
            download_root=downloads,
            notifications_enabled=True,
        )
        await self.repo.open()
        await self.repo.migrate()

    async def asyncTearDown(self) -> None:
        await self.repo.close()

    async def test_terminal_transition_enqueues_only_redacted_payload(self) -> None:
        job = await self.repo.accept_job(
            kind="url",
            user_id=99887766,
            state="downloading",
            source_kind="url",
            source_url="https://user:password@example.invalid/private",
            texts=["private caption"],
        )
        result = await self.repo.transition_job(
            job.id,
            expected_revision=job.revision,
            to_state="failed",
            event_type="failed",
            payload={
                "schema_version": 1,
                "error_code": "network_timeout",
                "error_message": "private failure detail",
            },
        )
        self.assertTrue(result.applied)
        record = await self.repo.claim_notification(owner="test", now=10**12)
        self.assertIsNotNone(record)
        self.assertEqual(record.event_type, "job.failed")
        rendered = json.dumps(record.payload)
        self.assertIn("network_timeout", rendered)
        for secret in (
            "99887766",
            "password",
            "example.invalid",
            "private caption",
            "private failure detail",
        ):
            self.assertNotIn(secret, rendered)

    async def test_payload_schema_and_dedupe_are_enforced(self) -> None:
        first = await self.repo.enqueue_notification(
            event_type="runtime.started",
            payload={"schema_version": 1, "service": "telegram_video_forwarder"},
            dedupe_key="runtime:test",
            now=10.0,
        )
        second = await self.repo.enqueue_notification(
            event_type="runtime.started",
            payload={"schema_version": 1, "service": "telegram_video_forwarder"},
            dedupe_key="runtime:test",
            now=11.0,
        )
        self.assertEqual(first.id, second.id)
        with self.assertRaises(RepositoryError):
            await self.repo.enqueue_notification(
                event_type="runtime.started",
                payload={"schema_version": 1, "secret": "must-not-enter-outbox"},
                dedupe_key="runtime:bad",
            )

    async def test_successful_publish_enqueues_one_success_event(self) -> None:
        media = Path(self.tempdir.name) / "downloads" / "job-1" / "video.mp4"
        media.parent.mkdir()
        media.write_bytes(b"media")
        job = await self.repo.accept_job(
            kind="telegram",
            user_id=42,
            state="queued",
            source_kind="telegram",
            items=[{"media_kind": "video"}],
        )
        download = await self.repo.claim_next_download(owner="download-test")
        ready = await self.repo.record_download_ready(
            job.id,
            [str(media)],
            expected_revision=download.job.revision,
        )
        publish = await self.repo.claim_next_publish(owner="publish-test")
        result = await self.repo.record_published_messages(
            job.id,
            [(-1001, 1, "channel")],
            expected_revision=publish.job.revision,
        )
        self.assertTrue(ready.applied)
        self.assertTrue(result.applied)
        record = await self.repo.claim_notification(owner="test", now=10**12)
        self.assertEqual(record.event_type, "job.succeeded")
        self.assertEqual(record.payload["state"], "succeeded")
        self.assertEqual((await self.repo.notification_outbox_counts())["delivering"], 1)

    async def test_expired_claim_is_recovered_without_duplicate_rows(self) -> None:
        await self.repo.enqueue_notification(
            event_type="runtime.started",
            payload={"schema_version": 1, "service": "telegram_video_forwarder"},
            dedupe_key="runtime:lease",
            now=1.0,
        )
        first = await self.repo.claim_notification(owner="worker-a", lease_seconds=5, now=1.0)
        self.assertEqual(first.attempt_count, 1)
        second = await self.repo.claim_notification(owner="worker-b", lease_seconds=5, now=7.0)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.attempt_count, 2)
        self.assertEqual((await self.repo.notification_outbox_counts())["delivering"], 1)


class WebhookNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        downloads = root / "downloads"
        downloads.mkdir()
        self.repo = SQLiteRepository(root / "state.sqlite3", download_root=downloads)
        await self.repo.open()
        await self.repo.migrate()
        self.clock = _Clock()
        self.token = "w" * 32

    async def asyncTearDown(self) -> None:
        await self.repo.close()

    async def _enqueue(self, suffix: str = "one") -> None:
        await self.repo.enqueue_notification(
            event_type="runtime.started",
            payload={
                "schema_version": 1,
                "service": "telegram_video_forwarder",
                "occurred_at": self.clock(),
            },
            dedupe_key=f"runtime:{suffix}",
            now=self.clock(),
        )

    async def test_delivery_is_hmac_signed_and_marks_sent(self) -> None:
        await self._enqueue()
        transport = _Transport(204)
        notifier = WebhookNotifier(
            self.repo,
            url="https://hooks.example.invalid/tvf",
            token=self.token,
            transport=transport,
            clock=self.clock,
            jitter=lambda _a, _b: 0.0,
        )
        self.assertTrue(await notifier.deliver_once())
        counts = await self.repo.notification_outbox_counts()
        self.assertEqual(counts["sent"], 1)
        _url, body, headers, _timeout = transport.calls[0]
        signed = headers["X-TVF-Timestamp"].encode() + b"." + body
        expected = hmac.new(self.token.encode(), signed, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-TVF-Signature"], f"sha256={expected}")
        envelope = json.loads(body)
        self.assertEqual(envelope["event_type"], "runtime.started")
        self.assertNotIn("token", json.dumps(envelope))

    async def test_retryable_status_backs_off_then_permanent_status_dies(self) -> None:
        await self._enqueue("retry")
        transport = _Transport(503, 400)
        notifier = WebhookNotifier(
            self.repo,
            url="https://hooks.example.invalid/tvf",
            token=self.token,
            max_attempts=3,
            transport=transport,
            clock=self.clock,
            jitter=lambda _a, _b: 0.0,
        )
        self.assertTrue(await notifier.deliver_once())
        self.assertEqual((await self.repo.notification_outbox_counts())["retry_wait"], 1)
        self.clock.value += 2.0
        self.assertTrue(await notifier.deliver_once())
        self.assertEqual((await self.repo.notification_outbox_counts())["dead"], 1)

    async def test_transport_errors_are_bounded_and_retryable(self) -> None:
        await self._enqueue("network")
        transport = _Transport(WebhookTransportError("network_error"))
        notifier = WebhookNotifier(
            self.repo,
            url="https://hooks.example.invalid/tvf",
            token=self.token,
            max_attempts=2,
            transport=transport,
            clock=self.clock,
            jitter=lambda _a, _b: 0.0,
        )
        self.assertTrue(await notifier.deliver_once())
        self.assertEqual((await self.repo.notification_outbox_counts())["retry_wait"], 1)


if __name__ == "__main__":
    unittest.main()
