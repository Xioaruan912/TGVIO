from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import tempfile
import time
import unittest

from tgvio.application.notifications import (
    NotificationRuntime,
    OutboxService,
    WebhookDeliveryError,
)
from tgvio.domain.notifications import NotificationEvent, OutboxState
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class _RecordingNotifier:
    def __init__(self, *, fail_codes: list[str] | None = None) -> None:
        self.delivered: list[dict[str, object]] = []
        self.fail_codes = list(fail_codes or [])

    async def deliver(self, payload: dict[str, object]) -> None:
        if self.fail_codes:
            raise WebhookDeliveryError(self.fail_codes.pop(0), "fail")
        self.delivered.append(payload)


class NotificationOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    def _event(self, key: str = "k1") -> NotificationEvent:
        return NotificationEvent(
            event_type="job.succeeded",
            dedupe_key=key,
            payload={"job_id": "job-1", "state": "succeeded", "media_count": 2, "bytes": 100},
        )

    async def test_enqueue_is_idempotent_by_dedupe_key(self) -> None:
        service = OutboxService(self.repo)
        self.assertTrue(await service.enqueue(self._event(), now=100.0))
        self.assertFalse(await service.enqueue(self._event(), now=101.0))
        counts = await self.repo.count_notification_outbox()
        self.assertEqual(counts["pending"], 1)

    async def test_payload_and_event_allowlist_reject_secrets(self) -> None:
        with self.assertRaises(ValueError):
            NotificationEvent(
                event_type="job.succeeded",
                dedupe_key="bad",
                payload={"caption": "secret"},
            )
        with self.assertRaises(ValueError):
            NotificationEvent(event_type="job.custom", dedupe_key="bad", payload={})

    async def test_claim_complete_fail_and_dead_letter(self) -> None:
        service = OutboxService(self.repo)
        await service.enqueue(self._event(), now=100.0)
        claimed = await self.repo.claim_due_notifications(
            now=100.0, holder_id="w1", limit=5
        )
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].state, OutboxState.CLAIMED)
        self.assertTrue(
            await self.repo.complete_notification(claimed[0].id, holder_id="w1", now=100.0)
        )
        self.assertEqual((await self.repo.count_notification_outbox())["sent"], 1)

        await service.enqueue(self._event("k2"), now=200.0)
        claimed = await self.repo.claim_due_notifications(now=200.0, holder_id="w2", limit=5)
        entry = await self.repo.fail_notification(
            claimed[0].id, holder_id="w2", error_code="webhook_http_500", now=200.0
        )
        assert entry is not None
        self.assertEqual(entry.state, OutboxState.PENDING)
        self.assertEqual(entry.attempts, 1)
        self.assertGreater(entry.next_attempt_at, 200.0)

    async def test_expired_claim_is_recovered(self) -> None:
        service = OutboxService(self.repo)
        await service.enqueue(self._event(), now=100.0)
        await self.repo.claim_due_notifications(
            now=100.0, holder_id="w1", limit=5, lease_seconds=10.0
        )
        claimed = await self.repo.claim_due_notifications(now=200.0, holder_id="w2", limit=5)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].claimed_by, "w2")

    async def test_feed_maps_terminal_jobs_and_archive_without_secrets(self) -> None:
        conn = self.repo._require()
        await conn.execute(
            "INSERT INTO jobs(id, owner_id, destination, state, policy_json, error_code) "
            "VALUES('job-9', 7, '@channel', 'failed', '{}', 'download_failed')"
        )
        await conn.execute("INSERT INTO job_schedule(job_id) VALUES('job-9')")
        await conn.commit()
        runtime = NotificationRuntime(self.repo, _RecordingNotifier(), now=lambda: time.time())
        result = await runtime.run_once()
        self.assertEqual(result["sent"], 1)
        candidates = await self.repo.get_notification_candidates(since_epoch=0)
        self.assertTrue(any(row["job_id"] == "job-9" for row in candidates))


class AlertFinalGateTests(unittest.TestCase):
    def _runtime(self) -> NotificationRuntime:
        return NotificationRuntime(object(), _RecordingNotifier())

    def test_transient_failures_are_not_alerted(self) -> None:
        runtime = self._runtime()
        self.assertIsNone(
            runtime._event_from_candidate(
                {
                    "kind": "job",
                    "job_id": "j1",
                    "state": "failed",
                    "error_code": "download_failed",
                    "recovery_status": "scheduled",
                }
            )
        )
        self.assertIsNone(
            runtime._event_from_candidate(
                {
                    "kind": "archive",
                    "package_id": "p1",
                    "job_id": "j1",
                    "state": "failed",
                    "recovery_status": "retrying",
                }
            )
        )

    def test_final_failures_are_alerted(self) -> None:
        runtime = self._runtime()
        job_event = runtime._event_from_candidate(
            {
                "kind": "job",
                "job_id": "j1",
                "state": "failed",
                "error_code": "download_failed",
                "recovery_status": "exhausted",
            }
        )
        self.assertIsNotNone(job_event)
        assert job_event is not None
        self.assertEqual(job_event.event_type, "job.failed")
        archive_event = runtime._event_from_candidate(
            {
                "kind": "archive",
                "package_id": "p1",
                "job_id": "j1",
                "state": "failed",
                "recovery_status": "quarantined",
            }
        )
        self.assertIsNotNone(archive_event)
        assert archive_event is not None
        self.assertEqual(archive_event.event_type, "archive.failed")

    def test_failure_without_recovery_decision_is_treated_as_final(self) -> None:
        runtime = self._runtime()
        event = runtime._event_from_candidate(
            {"kind": "job", "job_id": "j1", "state": "failed", "error_code": "x"}
        )
        self.assertIsNotNone(event)
