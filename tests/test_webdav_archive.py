from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import unittest

from tgvio.adapters.webdav_archive import (
    WebDavArchiveError,
    WebDavArchiveSafetyError,
    WebDavArchiveTransport,
)
from tgvio.domain.archive import ArchiveRemoteStat


class ProbeFixtureTransport(WebDavArchiveTransport):
    def __init__(self, *, allow: str, propfind_status: int = 207) -> None:
        super().__init__("https://dav.example.test/root", "user", "password")
        self.allow = allow
        self.propfind_status = propfind_status

    def _request_sync(self, method, absolute_path, *, body=None, headers=None):
        if method == "OPTIONS":
            return 200, {"allow": self.allow, "dav": "1, 2"}, b""
        if method == "PROPFIND":
            body = (
                b'<?xml version="1.0"?>'
                b'<D:multistatus xmlns:D="DAV:"><D:response><D:propstat><D:prop>'
                b'<D:getetag>"abc"</D:getetag>'
                b'<D:resourcetype><D:collection/></D:resourcetype>'
                b'<D:quota-used-bytes>123</D:quota-used-bytes>'
                b'<D:quota-available-bytes>456</D:quota-available-bytes>'
                b'</D:prop></D:propstat></D:response></D:multistatus>'
            )
            return self.propfind_status, {}, body
        raise AssertionError(f"unexpected method {method}")


class WebDavArchiveTransportTests(unittest.TestCase):
    def test_capability_probe_detects_move_etag_and_quota_without_write_probe(self) -> None:
        transport = ProbeFixtureTransport(
            allow="OPTIONS, GET, PUT, PROPFIND, MKCOL, MOVE"
        )
        capabilities = transport._probe_sync()
        self.assertTrue(capabilities.supports_propfind)
        self.assertTrue(capabilities.supports_mkcol)
        self.assertTrue(capabilities.supports_put)
        self.assertTrue(capabilities.supports_move)
        self.assertTrue(capabilities.supports_get)
        self.assertTrue(capabilities.supports_etag)
        self.assertTrue(capabilities.supports_quota)
        self.assertEqual(capabilities.quota_used_bytes, 123)
        self.assertEqual(capabilities.quota_available_bytes, 456)
        self.assertEqual(capabilities.commit_mode, "move")

    def test_missing_move_falls_back_to_complete_marker_commit_mode(self) -> None:
        transport = ProbeFixtureTransport(allow="GET, PUT, PROPFIND, MKCOL")
        capabilities = transport._probe_sync()
        self.assertFalse(capabilities.supports_move)
        self.assertEqual(capabilities.commit_mode, "complete_marker")

    def test_root_options_omission_falls_back_to_isolated_write_probe(self) -> None:
        class OmittedMethodsTransport(ProbeFixtureTransport):
            def __init__(self) -> None:
                super().__init__(allow="OPTIONS, PROPFIND, MOVE")
                self.files = {}
                self.collections = set()

            def _ensure_collection_sync(self, remote_path):
                self.collections.add(remote_path)

            def _request_sync(self, method, absolute_path, *, body=None, headers=None):
                if method in {"OPTIONS", "PROPFIND"} and absolute_path == self._base_path:
                    return super()._request_sync(method, absolute_path, body=body, headers=headers)
                if method == "PUT":
                    self.files[absolute_path] = bytes(body or b"")
                    return 201, {}, b""
                if method == "GET":
                    return 200, {}, self.files.get(absolute_path, b"")
                if method == "DELETE":
                    return 204, {}, b""
                raise AssertionError(f"unexpected method {method}")

            def _stat_sync(self, remote_path):
                absolute = self._absolute_path(remote_path)
                payload = self.files.get(absolute)
                return ArchiveRemoteStat(
                    exists=payload is not None,
                    size_bytes=len(payload) if payload is not None else None,
                    etag='"probe"' if payload is not None else None,
                )

            def _move_collection_sync(self, source_path, destination_path):
                source = self._absolute_path(f"{source_path}/probe.bin")
                destination = self._absolute_path(f"{destination_path}/probe.bin")
                self.files[destination] = self.files.pop(source)

        capabilities = OmittedMethodsTransport()._probe_sync()
        self.assertTrue(capabilities.supports_mkcol)
        self.assertTrue(capabilities.supports_put)
        self.assertTrue(capabilities.supports_get)
        self.assertTrue(capabilities.supports_move)
        self.assertTrue(capabilities.supports_etag)

    def test_archive_paths_reject_traversal_and_generate_safe_destination_url(self) -> None:
        transport = ProbeFixtureTransport(allow="GET, PUT, PROPFIND, MKCOL, MOVE")
        self.assertEqual(
            transport._absolute_path("archive/2026/file name.mp4"),
            "/root/archive/2026/file name.mp4",
        )
        self.assertEqual(
            transport._destination_url("archive/2026/file name.mp4"),
            "https://dav.example.test/root/archive/2026/file%20name.mp4",
        )
        with self.assertRaisesRegex(ValueError, "unsafe"):
            transport._absolute_path("archive/../secret")

    def test_lost_put_response_is_accepted_only_when_remote_size_is_confirmed(self) -> None:
        class LostResponseTransport(ProbeFixtureTransport):
            def __init__(self, *, confirmed: bool) -> None:
                super().__init__(allow="GET, PUT, PROPFIND, MKCOL")
                self.confirmed = confirmed
                self._verify_attempts = 1
                self._verify_interval_seconds = 0

            def _ensure_parent_sync(self, remote_path):
                return None

            def _stream_put_sync(self, local, absolute_path, size):
                raise TimeoutError("response lost")

            def _stat_sync(self, remote_path):
                return ArchiveRemoteStat(
                    exists=self.confirmed,
                    size_bytes=7 if self.confirmed else None,
                    etag='"confirmed"' if self.confirmed else None,
                )

        with TemporaryDirectory() as tmp:
            local = Path(tmp) / "file.bin"
            local.write_bytes(b"payload")
            receipt = LostResponseTransport(confirmed=True)._put_file_sync(
                local,
                "archive/file.bin",
                7,
            )
            self.assertEqual(receipt.size_bytes, 7)
            self.assertFalse(receipt.reused_remote)
            with self.assertRaises(WebDavArchiveError):
                LostResponseTransport(confirmed=False)._put_file_sync(
                    local,
                    "archive/file.bin",
                    7,
                )

    def test_lost_put_response_polls_until_remote_size_becomes_visible(self) -> None:
        class EventuallyVisibleTransport(ProbeFixtureTransport):
            def __init__(self) -> None:
                super().__init__(allow="GET, PUT, PROPFIND, MKCOL")
                self._verify_attempts = 3
                self._verify_interval_seconds = 0
                self.stats = 0

            def _ensure_parent_sync(self, remote_path):
                return None

            def _stream_put_sync(self, local, absolute_path, size):
                raise TimeoutError("response lost")

            def _stat_sync(self, remote_path):
                self.stats += 1
                if self.stats < 3:
                    return ArchiveRemoteStat(exists=False)
                return ArchiveRemoteStat(exists=True, size_bytes=7, etag='"late"')

        with TemporaryDirectory() as tmp:
            local = Path(tmp) / "file.bin"
            local.write_bytes(b"payload")
            transport = EventuallyVisibleTransport()
            receipt = transport._put_file_sync(local, "archive/file.bin", 7)
            self.assertEqual(transport.stats, 3)
            self.assertEqual(receipt.size_bytes, 7)
            self.assertEqual(receipt.etag, '"late"')

    def test_exact_file_delete_verifies_receipt_and_uses_if_match(self) -> None:
        class DeleteTransport(ProbeFixtureTransport):
            def __init__(self) -> None:
                super().__init__(allow="DELETE, GET, PUT, PROPFIND, MKCOL")
                self.present = True
                self.calls = []
                self._verify_attempts = 1
                self._verify_interval_seconds = 0

            def _stat_sync(self, remote_path):
                if not self.present:
                    return ArchiveRemoteStat(exists=False)
                return ArchiveRemoteStat(
                    exists=True,
                    size_bytes=7,
                    etag='"file-etag"',
                    is_collection=False,
                )

            def _get_bytes_sync(self, remote_path, max_bytes):
                return b"payload"

            def _request_sync(self, method, absolute_path, *, body=None, headers=None):
                self.calls.append((method, absolute_path, headers))
                if method != "DELETE":
                    raise AssertionError(f"unexpected method {method}")
                self.present = False
                return 204, {}, b""

        transport = DeleteTransport()
        receipt = transport._delete_file_sync(
            "archive/package/media/file.bin",
            7,
            '"file-etag"',
            hashlib.sha256(b"payload").hexdigest(),
        )
        self.assertEqual(receipt.remote_path, "archive/package/media/file.bin")
        self.assertEqual(receipt.verification_method, "absent_after_delete")
        self.assertFalse(receipt.already_missing)
        self.assertEqual(
            transport.calls,
            [
                (
                    "DELETE",
                    "/root/archive/package/media/file.bin",
                    {"If-Match": '"file-etag"'},
                )
            ],
        )

    def test_missing_exact_file_is_idempotent_without_sending_delete(self) -> None:
        class MissingTransport(ProbeFixtureTransport):
            def __init__(self) -> None:
                super().__init__(allow="DELETE, PROPFIND")
                self.calls = []

            def _stat_sync(self, remote_path):
                return ArchiveRemoteStat(exists=False)

            def _request_sync(self, method, absolute_path, *, body=None, headers=None):
                self.calls.append((method, absolute_path))
                raise AssertionError("DELETE must not be sent for an already absent file")

        transport = MissingTransport()
        receipt = transport._delete_file_sync("archive/package/manifest.json", 12, None, None)
        self.assertTrue(receipt.already_missing)
        self.assertEqual(receipt.verification_method, "already_absent")
        self.assertEqual(transport.calls, [])

    def test_delete_refuses_collection_size_etag_and_content_conflicts(self) -> None:
        class ConflictTransport(ProbeFixtureTransport):
            def __init__(self) -> None:
                super().__init__(allow="DELETE, GET, PROPFIND")
                self.stat_value = ArchiveRemoteStat(
                    exists=True,
                    size_bytes=7,
                    etag='"etag"',
                )
                self.payload = b"payload"
                self.delete_calls = 0

            def _stat_sync(self, remote_path):
                return self.stat_value

            def _get_bytes_sync(self, remote_path, max_bytes):
                return self.payload

            def _request_sync(self, method, absolute_path, *, body=None, headers=None):
                self.delete_calls += 1
                raise AssertionError("unsafe target must never reach DELETE")

        transport = ConflictTransport()
        transport.stat_value = ArchiveRemoteStat(
            exists=True,
            size_bytes=0,
            etag='"directory"',
            is_collection=True,
        )
        with self.assertRaisesRegex(WebDavArchiveSafetyError, "collection"):
            transport._delete_file_sync("archive/package/media", 0, None, None)

        transport.stat_value = ArchiveRemoteStat(exists=True, size_bytes=8, etag='"etag"')
        with self.assertRaisesRegex(WebDavArchiveSafetyError, "size"):
            transport._delete_file_sync("archive/package/file.bin", 7, None, None)

        transport.stat_value = ArchiveRemoteStat(exists=True, size_bytes=7, etag='"changed"')
        with self.assertRaisesRegex(WebDavArchiveSafetyError, "ETag"):
            transport._delete_file_sync(
                "archive/package/file.bin",
                7,
                '"expected"',
                None,
            )

        transport.stat_value = ArchiveRemoteStat(exists=True, size_bytes=7, etag='"etag"')
        transport.payload = b"changed"
        with self.assertRaisesRegex(WebDavArchiveSafetyError, "content"):
            transport._delete_file_sync(
                "archive/package/manifest.json",
                7,
                None,
                hashlib.sha256(b"payload").hexdigest(),
            )
        self.assertEqual(transport.delete_calls, 0)

    def test_expected_sha256_unreadable_never_sends_delete(self) -> None:
        class UnreadableTransport(ProbeFixtureTransport):
            def __init__(self) -> None:
                super().__init__(allow="DELETE, GET, PROPFIND")
                self.delete_calls = 0

            def _stat_sync(self, remote_path):
                return ArchiveRemoteStat(
                    exists=True,
                    size_bytes=7,
                    etag='"etag"',
                    is_collection=False,
                )

            def _get_bytes_sync(self, remote_path, max_bytes):
                return None

            def _request_sync(self, method, absolute_path, *, body=None, headers=None):
                if method == "DELETE":
                    self.delete_calls += 1
                raise AssertionError("DELETE must not be sent when content cannot be verified")

        transport = UnreadableTransport()
        with self.assertRaisesRegex(WebDavArchiveSafetyError, "could not be verified"):
            transport._delete_file_sync(
                "archive/package/manifest.json",
                7,
                '"etag"',
                hashlib.sha256(b"payload").hexdigest(),
            )
        self.assertEqual(transport.delete_calls, 0)

    def test_lost_delete_response_is_success_only_after_absence_is_confirmed(self) -> None:
        class LostDeleteTransport(ProbeFixtureTransport):
            def __init__(self, *, removed: bool) -> None:
                super().__init__(allow="DELETE, PROPFIND")
                self.present = True
                self.removed = removed
                self._verify_attempts = 1
                self._verify_interval_seconds = 0

            def _stat_sync(self, remote_path):
                return ArchiveRemoteStat(
                    exists=self.present,
                    size_bytes=7 if self.present else None,
                    etag='"etag"' if self.present else None,
                )

            def _request_sync(self, method, absolute_path, *, body=None, headers=None):
                if self.removed:
                    self.present = False
                raise TimeoutError("fixture response lost")

        receipt = LostDeleteTransport(removed=True)._delete_file_sync(
            "archive/package/file.bin",
            7,
            None,
            None,
        )
        self.assertEqual(receipt.verification_method, "absent_after_lost_response")
        with self.assertRaises(WebDavArchiveError):
            LostDeleteTransport(removed=False)._delete_file_sync(
                "archive/package/file.bin",
                7,
                None,
                None,
            )

    def test_delete_rejects_root_traversal_and_unverified_success(self) -> None:
        class StickyTransport(ProbeFixtureTransport):
            def __init__(self) -> None:
                super().__init__(allow="DELETE, PROPFIND")
                self._verify_attempts = 1
                self._verify_interval_seconds = 0

            def _stat_sync(self, remote_path):
                return ArchiveRemoteStat(exists=True, size_bytes=7, etag=None)

            def _request_sync(self, method, absolute_path, *, body=None, headers=None):
                return 204, {}, b""

        transport = StickyTransport()
        for unsafe in ("", "/", "../file", "archive/%2e%2e/file"):
            with self.subTest(path=unsafe):
                with self.assertRaises((ValueError, WebDavArchiveSafetyError)):
                    transport._delete_file_sync(unsafe, 7, None, None)
        with self.assertRaisesRegex(WebDavArchiveError, "verified"):
            transport._delete_file_sync("archive/package/file.bin", 7, None, None)
