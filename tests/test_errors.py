import asyncio
import errno
import unittest

from src.domain import ErrorCode, RetryPolicy, classify_error, safe_traceback
from src.webdav import WebDavUploadError


class NamedError(Exception):
    pass


def error_named(name: str, message: str = "") -> Exception:
    return type(name, (NamedError,), {})(message)


class ErrorClassifierTests(unittest.TestCase):
    def test_network_timeout_and_disk_full_are_retryable(self) -> None:
        timeout = classify_error(TimeoutError("secret endpoint"), stage="download")
        disk = classify_error(OSError(errno.ENOSPC, "token=secret"), stage="download")
        self.assertEqual(timeout.code, ErrorCode.NETWORK_TIMEOUT)
        self.assertEqual(disk.code, ErrorCode.DISK_LOW)
        self.assertTrue(timeout.retryable)
        self.assertNotIn("secret", timeout.summary)

    def test_telegram_terminal_errors_have_specific_actions(self) -> None:
        auth = classify_error(error_named("AuthKeyUnregisteredError"), stage="publish")
        permission = classify_error(error_named("ChatWriteForbiddenError"), stage="publish")
        expired = classify_error(error_named("FileReferenceExpiredError"), stage="download")
        self.assertEqual(auth.code, ErrorCode.TELEGRAM_AUTH)
        self.assertEqual(permission.code, ErrorCode.TELEGRAM_PERMISSION)
        self.assertEqual(expired.code, ErrorCode.SOURCE_EXPIRED)
        self.assertFalse(auth.retryable)
        self.assertFalse(permission.retryable)

    def test_webdav_statuses_and_url_unsupported_are_classified(self) -> None:
        locked = error_named("WebDavError")
        locked.status_code = 423
        self.assertEqual(classify_error(locked, stage="backup").code, ErrorCode.WEBDAV_LOCKED)
        unsupported = classify_error(RuntimeError("Unsupported URL: private"), stage="download")
        self.assertEqual(unsupported.code, ErrorCode.URL_UNSUPPORTED)
        self.assertFalse(unsupported.retryable)

    def test_webdav_upload_error_statuses_have_backup_specific_policy(self) -> None:
        auth = classify_error(WebDavUploadError("hidden", status=401), stage="backup")
        locked = classify_error(WebDavUploadError("hidden", status=423), stage="backup")
        server = classify_error(WebDavUploadError("hidden", status=503), stage="backup")
        self.assertEqual(auth.code, ErrorCode.WEBDAV_AUTH)
        self.assertFalse(auth.retryable)
        self.assertEqual(locked.code, ErrorCode.WEBDAV_LOCKED)
        self.assertTrue(locked.retryable)
        self.assertEqual(server.code, ErrorCode.WEBDAV_SERVER)
        self.assertTrue(server.retryable)

    def test_cancelled_is_never_failed_or_retried(self) -> None:
        result = classify_error(asyncio.CancelledError(), stage="download")
        self.assertEqual(result.code, ErrorCode.CANCELLED)
        self.assertFalse(result.retryable)

    def test_safe_traceback_omits_exception_message(self) -> None:
        try:
            raise RuntimeError("token=top-secret https://user:pass@example.invalid")
        except RuntimeError as exc:
            rendered = safe_traceback(exc)
        self.assertIn("test_safe_traceback_omits_exception_message", rendered)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("user:pass", rendered)


class RetryPolicyTests(unittest.TestCase):
    def test_exponential_backoff_jitter_and_budget(self) -> None:
        policy = RetryPolicy(budgets={"download": 2}, jitter=lambda: 0.25)
        error = classify_error(TimeoutError(), stage="download")
        first = policy.decide(error, stage="download", attempt=1, now=100.0)
        second = policy.decide(error, stage="download", attempt=2, now=100.0)
        exhausted = policy.decide(error, stage="download", attempt=3, now=100.0)
        self.assertEqual(first.delay_seconds, 5.25)
        self.assertEqual(first.next_retry_at, 105.25)
        self.assertEqual(second.delay_seconds, 10.25)
        self.assertFalse(exhausted.should_retry)

    def test_flood_wait_uses_server_delay_without_exponential_backoff(self) -> None:
        flood = error_named("FloodWaitError")
        flood.seconds = 37
        error = classify_error(flood, stage="publish")
        decision = RetryPolicy(jitter=lambda: 0.99).decide(
            error, stage="publish", attempt=2, now=10.0
        )
        self.assertEqual(error.code, ErrorCode.TELEGRAM_FLOOD_WAIT)
        self.assertEqual(decision.delay_seconds, 38.0)
        self.assertEqual(decision.next_retry_at, 48.0)

    def test_backup_backoff_uses_separate_longer_budget(self) -> None:
        error = classify_error(WebDavUploadError("hidden", status=503), stage="backup")
        policy = RetryPolicy(budgets={"backup": 2}, jitter=lambda: 0.5)
        first = policy.decide(error, stage="backup", attempt=1, now=100.0)
        second = policy.decide(error, stage="backup", attempt=2, now=100.0)
        self.assertEqual(first.delay_seconds, 60.5)
        self.assertEqual(second.delay_seconds, 120.5)
