from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Protocol

from tgvio.application.auto_recovery import recovery_status_is_final
from tgvio.application.ports import JobRepository
from tgvio.domain.notifications import NotificationEvent, new_holder_id
from tgvio.observability import log_event


_TERMINAL_WINDOW_SECONDS = 24 * 60 * 60


class WebhookDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# Generic delivery failure for any outbox channel (Telegram owner DM, webhook).
NotificationDeliveryError = WebhookDeliveryError


class Notifier(Protocol):
    async def deliver(self, payload: dict[str, object]) -> None: ...


class WebhookNotifier:
    """Deliver one redacted event to an HTTPS endpoint with an HMAC signature.

    The endpoint URL and token are never logged or persisted; only a normalized
    error code is returned to the outbox.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_seconds: float = 10.0,
        sender: Callable[[str, dict[str, str], bytes, float], None] | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise WebhookDeliveryError("webhook_not_https", "webhook URL must be HTTPS")
        if parsed.username or parsed.password:
            raise WebhookDeliveryError(
                "webhook_url_credentials",
                "webhook URL must not contain embedded credentials",
            )
        if not token:
            raise WebhookDeliveryError("webhook_token_missing", "webhook token is required")
        self._url = url.strip()
        self._token = token
        self._timeout = max(1.0, float(timeout_seconds))
        self._sender = sender or self._send_http

    async def deliver(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(
            self._token.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "tgvio-notifier/1",
            "X-TGVIO-Signature": f"sha256={signature}",
        }
        try:
            await asyncio.to_thread(self._sender, self._url, headers, body, self._timeout)
        except WebhookDeliveryError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized to a safe code
            raise WebhookDeliveryError("webhook_transport", type(exc).__name__) from exc

    def _send_http(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> None:
        import http.client

        parsed = urllib.parse.urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=timeout,
        )
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            response.read()
            if not 200 <= response.status < 300:
                raise WebhookDeliveryError(f"webhook_http_{response.status}")
        finally:
            connection.close()


class OutboxService:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    async def enqueue(self, event: NotificationEvent, *, now: float | None = None) -> bool:
        return await self._repository.enqueue_notification(
            event,
            now=float(time.time() if now is None else now),
        )


class NotificationRuntime:
    """Idempotently mirror terminal events into the outbox and dispatch them."""

    def __init__(
        self,
        repository: JobRepository,
        notifier: Notifier,
        *,
        secondary_notifier: Notifier | None = None,
        primary_accepts: frozenset[str] | None = None,
        poll_seconds: float = 15.0,
        max_attempts: int = 5,
        base_seconds: float = 30.0,
        cap_seconds: float = 3600.0,
        batch_size: int = 10,
        holder_id: str | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._repository = repository
        self._notifier = notifier
        self._secondary = secondary_notifier
        self._primary_accepts = primary_accepts
        self._poll_seconds = max(1.0, float(poll_seconds))
        self._max_attempts = max(1, int(max_attempts))
        self._base_seconds = max(1.0, float(base_seconds))
        self._cap_seconds = max(self._base_seconds, float(cap_seconds))
        self._batch_size = max(1, int(batch_size))
        self._holder_id = holder_id or new_holder_id()
        self._now = now or time.time
        self._task: asyncio.Task | None = None
        self._log = logging.getLogger("tgvio.notifications")

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="tgvio-notification-runtime")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def run_once(self) -> dict[str, int]:
        now = float(self._now())
        await self._enqueue_feed(now)
        entries = await self._repository.claim_due_notifications(
            now=now,
            holder_id=self._holder_id,
            limit=self._batch_size,
            lease_seconds=max(60.0, self._poll_seconds * 4),
        )
        sent = 0
        failed = 0
        for entry in entries:
            try:
                await self._deliver_entry(entry)
            except WebhookDeliveryError as exc:
                failed += 1
                await self._repository.fail_notification(
                    entry.id,
                    holder_id=self._holder_id,
                    error_code=exc.code,
                    now=float(self._now()),
                    base_seconds=self._base_seconds,
                    cap_seconds=self._cap_seconds,
                )
                log_event(
                    self._log,
                    logging.WARNING,
                    "notification.delivery.failed",
                    notification_id=entry.id,
                    event_type=entry.event_type,
                    error_code=exc.code,
                )
            else:
                sent += 1
                await self._repository.complete_notification(
                    entry.id,
                    holder_id=self._holder_id,
                    now=float(self._now()),
                )
                log_event(
                    self._log,
                    logging.INFO,
                    "notification.delivery.sent",
                    notification_id=entry.id,
                    event_type=entry.event_type,
                )
        return {"claimed": len(entries), "sent": sent, "failed": failed}

    async def _deliver_entry(self, entry) -> None:
        """Deliver one outbox entry.

        The primary channel is authoritative (its failure retries the entry).
        The secondary channel is best-effort fire-and-forget so a webhook outage
        never blocks the owner alert. Event scoping lets the owner channel carry
        only failures/anomalies while a webhook can receive the full stream.
        """
        primary_accepts = (
            self._primary_accepts is None or entry.event_type in self._primary_accepts
        )
        delivered = False
        if primary_accepts:
            await self._notifier.deliver(entry.delivery_signature)
            delivered = True
        if self._secondary is not None:
            try:
                await self._secondary.deliver(entry.delivery_signature)
                delivered = True
            except WebhookDeliveryError as exc:
                log_event(
                    self._log,
                    logging.WARNING,
                    "notification.secondary.failed",
                    notification_id=entry.id,
                    event_type=entry.event_type,
                    error_code=exc.code,
                )
        # No channel accepted this event; nothing to deliver, so settle it.
        _ = delivered

    async def _enqueue_feed(self, now: float) -> int:
        candidates = await self._repository.get_notification_candidates(
            since_epoch=int(now) - _TERMINAL_WINDOW_SECONDS,
        )
        enqueued = 0
        for candidate in candidates:
            event = self._event_from_candidate(candidate)
            if event is None:
                continue
            if await self._repository.enqueue_notification(event, now=now):
                enqueued += 1
        return enqueued

    def _event_from_candidate(self, candidate: dict[str, object]) -> NotificationEvent | None:
        kind = str(candidate.get("kind", ""))
        state = str(candidate.get("state", ""))
        recovery_status = candidate.get("recovery_status")
        # Only alert on a *final* failure. A transient failure that automatic
        # recovery will retry (scheduled/retrying) must not page the owner.
        if state == "failed" and recovery_status and not recovery_status_is_final(recovery_status):
            return None
        error_code = candidate.get("error_code")
        occurred_at = candidate.get("occurred_at")
        media_count = candidate.get("media_count")
        bytes_total = candidate.get("bytes")
        if kind == "job":
            event_type = {
                "succeeded": "job.succeeded",
                "failed": "job.failed",
                "cancelled": "job.cancelled",
            }.get(state)
            if event_type is None:
                return None
            job_id = str(candidate.get("job_id", ""))
            if not job_id:
                return None
            payload: dict[str, object] = {
                "job_id": job_id,
                "state": state,
                "media_count": int(media_count or 0),
                "bytes": int(bytes_total or 0),
                "occurred_at": int(occurred_at or 0),
            }
            if candidate.get("accepted_order") is not None:
                payload["accepted_order"] = int(candidate["accepted_order"])
            if error_code:
                payload["error_code"] = str(error_code)[:120]
            return NotificationEvent(
                event_type=event_type,
                dedupe_key=f"job:{job_id}:{state}",
                payload=payload,
                max_attempts=self._max_attempts,
            )
        if kind == "archive":
            event_type = {
                "committed": "archive.committed",
                "failed": "archive.failed",
            }.get(state)
            if event_type is None:
                return None
            package_id = str(candidate.get("package_id", ""))
            if not package_id:
                return None
            payload = {
                "package_id": package_id,
                "state": state,
                "media_count": int(media_count or 0),
                "bytes": int(bytes_total or 0),
                "occurred_at": int(occurred_at or 0),
            }
            job_id = candidate.get("job_id")
            if job_id:
                payload["job_id"] = str(job_id)
            if error_code:
                payload["error_code"] = str(error_code)[:120]
            return NotificationEvent(
                event_type=event_type,
                dedupe_key=f"archive:{package_id}:{state}",
                payload=payload,
                max_attempts=self._max_attempts,
            )
        return None

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "notification.runtime.failed",
                    "Notification runtime iteration failed",
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )
            await asyncio.sleep(self._poll_seconds)
