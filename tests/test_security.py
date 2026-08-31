import io
import logging
import os
from pathlib import Path
import stat
import tempfile
import unittest

from src.domain import ErrorCode, classify_error
from src.security import (
    RedactingFormatter,
    enforce_url_policy,
    inspect_http_url,
    normalize_remote_path,
    redact_text,
    safe_url_label,
    sanitize_filename,
    secure_private_directory,
    secure_private_file,
    validate_remote_name,
    validate_webdav_url,
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


class RuntimeBoundarySecurityTests(unittest.TestCase):
    def test_webdav_url_labels_and_paths_never_expose_credentials_or_traverse(self) -> None:
        raw = "https://alice:secret@DAV.Example.test:8443/root?token=private#fragment"
        label = safe_url_label(raw)
        self.assertEqual(label, "https://dav.example.test:8443/root")
        self.assertNotIn("alice", label)
        self.assertNotIn("secret", label)
        self.assertNotIn("token", label)

        self.assertEqual(
            validate_webdav_url("https://DAV.Example.test:8443/root"),
            "https://dav.example.test:8443/root",
        )
        for invalid in (
            raw,
            "https://dav.example.test/root?token=x",
            "file:///tmp/dav",
            "https://dav.example.test/%2e%2e/private",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_webdav_url(invalid)

        self.assertEqual(normalize_remote_path("/archive/2026"), "/archive/2026")
        for invalid in ("../private", "/safe/%2e%2e/private", r"safe\private"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_remote_path(invalid)
        for invalid in ("../video.mp4", "folder/video.mp4", "%2e%2e", "a%2fb.mp4"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_remote_name(invalid)

    def test_untrusted_local_filename_is_reduced_to_one_segment(self) -> None:
        self.assertEqual(sanitize_filename("../../.env"), "env")
        self.assertEqual(sanitize_filename(r"..\..\secret.mp4"), "secret.mp4")
        self.assertEqual(sanitize_filename("\x00\x01"), "media.bin")

    def test_private_runtime_permissions_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / "session"
            self.assertTrue(secure_private_directory(private_dir))
            secret = private_dir / "secret.json"
            secret.write_text("{}", encoding="utf-8")
            os.chmod(secret, 0o666)
            self.assertTrue(secure_private_file(secret))
            self.assertEqual(stat.S_IMODE(private_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
