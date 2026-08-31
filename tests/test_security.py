import io
import logging
import unittest

from src.domain import ErrorCode, classify_error
from src.security import (
    RedactingFormatter,
    enforce_url_policy,
    inspect_http_url,
    redact_text,
)


def _resolver_for(*addresses: str):
    def resolve(_host, _port, **_kwargs):
        return [
            (2, 1, 6, "", (address, 443))
            for address in addresses
        ]

    return resolve


class UrlSecurityTests(unittest.TestCase):
    def test_http_url_classifies_public_and_private_targets(self) -> None:
        public = inspect_http_url(
            "https://example.test/video",
            resolver=_resolver_for("93.184.216.34"),
        )
        private = inspect_http_url(
            "http://example.test/video",
            resolver=_resolver_for("127.0.0.1", "10.0.0.5"),
        )
        self.assertFalse(public.private_network)
        self.assertTrue(private.private_network)
        self.assertEqual(public.hostname, "example.test")

    def test_block_policy_rejects_private_and_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "private-network"):
            enforce_url_policy(
                "http://10.0.0.1/video",
                private_network_policy="block",
                resolver=_resolver_for("10.0.0.1"),
            )
        with self.assertRaisesRegex(ValueError, "credentials"):
            inspect_http_url(
                "https://user:secret@example.test/video",
                resolver=_resolver_for("93.184.216.34"),
            )
        with self.assertRaisesRegex(ValueError, "http/https"):
            inspect_http_url("file:///etc/passwd")

    def test_warn_policy_preserves_private_target_classification(self) -> None:
        result = enforce_url_policy(
            "http://192.168.1.2/video",
            private_network_policy="warn",
            resolver=_resolver_for("192.168.1.2"),
        )
        self.assertTrue(result.private_network)

    def test_warn_policy_tolerates_dns_failure(self) -> None:
        def broken_resolver(*_args, **_kwargs):
            raise OSError("dns unavailable")

        result = enforce_url_policy(
            "https://example.test/video",
            private_network_policy="warn",
            resolver=broken_resolver,
        )
        self.assertEqual(result.addresses, ())

    def test_block_policy_fails_closed_on_dns_failure(self) -> None:
        def broken_resolver(*_args, **_kwargs):
            raise OSError("dns unavailable")

        with self.assertRaisesRegex(ValueError, "resolution failed"):
            enforce_url_policy(
                "https://example.test/video",
                private_network_policy="block",
                resolver=broken_resolver,
            )

    def test_blocked_private_url_is_terminal_download_error(self) -> None:
        try:
            enforce_url_policy(
                "http://127.0.0.1/video",
                private_network_policy="block",
            )
        except ValueError as exc:
            info = classify_error(exc, stage="download")
        else:
            self.fail("private target unexpectedly allowed")
        self.assertEqual(info.code, ErrorCode.URL_UNSUPPORTED)
        self.assertFalse(info.retryable)


class LoggingSecurityTests(unittest.TestCase):
    def test_redact_text_covers_urls_headers_tokens_and_queries(self) -> None:
        raw = (
            "http://alice:secret@example.test/path?token=abc123 "
            "Authorization: Bearer top-secret BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyzABCDEFG"
        )
        rendered = redact_text(raw)
        for secret in ("alice:secret", "abc123", "top-secret", "abcdefghijklmnopqrstuvwxyz"):
            self.assertNotIn(secret, rendered)
        self.assertIn("example.test", rendered)

    def test_formatter_redacts_exception_traceback_text(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
        logger = logging.Logger("security-test")
        logger.addHandler(handler)
        try:
            raise RuntimeError("Authorization: Bearer secret-value")
        except RuntimeError:
            logger.exception("request failed url=https://u:p@example.test/x?token=secret-query")
        output = stream.getvalue()
        self.assertNotIn("secret-value", output)
        self.assertNotIn("secret-query", output)
        self.assertNotIn("u:p", output)
        self.assertIn("example.test", output)


if __name__ == "__main__":
    unittest.main()
