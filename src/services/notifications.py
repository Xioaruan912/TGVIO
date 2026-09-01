"""Durable, privacy-bounded webhook delivery for O1."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookResponse:
    status: int


class WebhookTransportError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibWebhookTransport:
    """HTTPS transport whose blocking standard-library call runs off-loop."""

    @staticmethod
    def _send_sync(
        url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> WebhookResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=float(timeout)) as response:
                # Consume a small bounded prefix so connections can be reused/closed cleanly.
                response.read(4096)
                return WebhookResponse(int(response.status))
        except urllib.error.HTTPError as exc:
            try:
                exc.read(4096)
            finally:
                exc.close()
            return WebhookResponse(int(exc.code))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WebhookTransportError("network_error") from exc

    async def send(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> WebhookResponse:
        return await asyncio.to_thread(self._send_sync, url, body, headers, timeout)


class WebhookNotifier:
    """Claim and deliver outbox rows without holding a database transaction over IO."""

    def __init__(
        self,
        repository: Any,
        *,
        url: str,
        token: str,
        timeout: float = 10.0,
        max_attempts: int = 8,
        poll_interval: float = 2.0,
        transport: Any | None = None,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        try:
            parsed = urlsplit(str(url))
            parsed_port = parsed.port
        except ValueError as exc:
            raise ValueError("webhook URL must use HTTPS") from exc
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed_port == 0
        ):
            raise ValueError("webhook URL must use HTTPS")
        encoded_token = str(token).encode("utf-8")
        if (
            len(encoded_token) < 32
            or len(encoded_token) > 512
            or any(byte < 33 or byte > 126 for byte in encoded_token)
        ):
            raise ValueError("webhook token must contain at least 32 bytes")
        self._repository = repository
        self._url = str(url)
        self._token = str(token).encode("utf-8")
        self._timeout = max(1.0, min(float(timeout), 60.0))
        self._max_attempts = max(1, min(int(max_attempts), 20))
        self._poll_interval = max(0.2, min(float(poll_interval), 60.0))
        self._transport = transport or UrllibWebhookTransport()
        self._clock = clock
        self._jitter = jitter
        self._owner = f"webhook:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_prune = 0.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        await self._repository.prune_notification_outbox(
            before=self._clock() - 30 * 86400,
            limit=500,
        )
        self._last_prune = self._clock()
        self._task = asyncio.create_task(self._run(), name="webhook-outbox")
        logger.info("Webhook outbox dispatcher started")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=self._timeout + 2.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        released = await self._repository.release_notification_claims(
            owner=self._owner,
            now=self._clock(),
        )
        if released:
            logger.info("Webhook dispatcher released %d interrupted claim(s)", released)

    async def enqueue_runtime_event(self, event: str) -> int:
        if event not in {"started", "stopping"}:
            raise ValueError("unsupported runtime notification")
        now = self._clock()
        record = await self._repository.enqueue_notification(
            event_type=f"runtime.{event}",
            payload={
                "schema_version": 1,
                "service": "telegram_video_forwarder",
                "occurred_at": now,
            },
            dedupe_key=f"runtime:{event}:{os.getpid()}:{int(now * 1000)}",
            now=now,
        )
        self._wake.set()
        return record.id

    @staticmethod
    def _body(record: Any) -> bytes:
        envelope = {
            "schema_version": 1,
            "event_id": int(record.id),
            "event_type": str(record.event_type),
            "occurred_at": float(record.created_at),
            "data": record.payload,
        }
        return json.dumps(
            envelope,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")

    def _headers(self, record: Any, body: bytes) -> dict[str, str]:
        timestamp = str(int(self._clock()))
        signature = hmac.new(
            self._token,
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "User-Agent": "telegram-video-forwarder-webhook/1",
            "X-TVF-Event": str(record.event_type),
            "X-TVF-Timestamp": timestamp,
            "X-TVF-Signature": f"sha256={signature}",
        }

    def _next_attempt_at(self, attempt_count: int) -> float:
        base = min(300.0, float(2 ** max(0, min(int(attempt_count) - 1, 8))))
        return self._clock() + base + max(0.0, float(self._jitter(0.0, base * 0.2)))

    async def deliver_once(self) -> bool:
        record = await self._repository.claim_notification(
            owner=self._owner,
            lease_seconds=max(30.0, self._timeout * 2.0),
            now=self._clock(),
        )
        if record is None:
            return False
        if record.attempt_count > self._max_attempts:
            await self._repository.fail_notification(
                record.id,
                owner=self._owner,
                error_code="attempts_exhausted",
                status=None,
                retryable=False,
                max_attempts=self._max_attempts,
                next_attempt_at=self._clock(),
                now=self._clock(),
            )
            logger.warning(
                "Webhook delivery exhausted outbox=%d event=%s state=dead",
                record.id,
                record.event_type,
            )
            return True
        body = self._body(record)
        headers = self._headers(record, body)
        try:
            response = await self._transport.send(
                self._url,
                body,
                headers,
                self._timeout,
            )
            status = int(response.status)
            if 200 <= status < 300:
                await self._repository.complete_notification(
                    record.id,
                    owner=self._owner,
                    status=status,
                    now=self._clock(),
                )
                logger.info(
                    "Webhook delivered outbox=%d event=%s status_class=%dxx",
                    record.id,
                    record.event_type,
                    status // 100,
                )
                return True
            retryable = status in {408, 425, 429} or 500 <= status < 600
            state = await self._repository.fail_notification(
                record.id,
                owner=self._owner,
                error_code=f"http_{status}",
                status=status,
                retryable=retryable,
                max_attempts=self._max_attempts,
                next_attempt_at=self._next_attempt_at(record.attempt_count),
                now=self._clock(),
            )
            logger.warning(
                "Webhook delivery deferred outbox=%d event=%s status_class=%dxx state=%s",
                record.id,
                record.event_type,
                status // 100,
                state,
            )
            return True
        except WebhookTransportError as exc:
            state = await self._repository.fail_notification(
                record.id,
                owner=self._owner,
                error_code=exc.code,
                status=None,
                retryable=True,
                max_attempts=self._max_attempts,
                next_attempt_at=self._next_attempt_at(record.attempt_count),
                now=self._clock(),
            )
            logger.warning(
                "Webhook delivery deferred outbox=%d event=%s status_class=network state=%s",
                record.id,
                record.event_type,
                state,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            state = await self._repository.fail_notification(
                record.id,
                owner=self._owner,
                error_code="transport_error",
                status=None,
                retryable=True,
                max_attempts=self._max_attempts,
                next_attempt_at=self._next_attempt_at(record.attempt_count),
                now=self._clock(),
            )
            logger.warning(
                "Webhook transport failed outbox=%d event=%s state=%s",
                record.id,
                record.event_type,
                state,
            )
            return True

    async def _wait(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                now = self._clock()
                if now - self._last_prune >= 3600.0:
                    await self._repository.prune_notification_outbox(
                        before=now - 30 * 86400,
                        limit=500,
                    )
                    self._last_prune = now
                delivered = await self.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Webhook dispatcher iteration failed")
                delivered = False
            if not delivered:
                await self._wait()
