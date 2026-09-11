from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.adapters.webdav_archive import WebDavArchiveError, WebDavArchiveTransport
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

