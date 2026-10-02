from __future__ import annotations

import unittest

from tgvio.adapters.webdav_archive import WebDavArchiveError, WebDavArchiveTransport
from tgvio.domain.archive import ArchiveRemoteStat


class MetadataResultFixture(WebDavArchiveTransport):
    """A DAV fixture can commit the payload before reporting a failed status."""

    def __init__(self, *, stored: bytes | None, read_error: Exception | None = None):
        super().__init__("https://fixture.invalid", "fixture", "fixture")
        self.stored = stored
        self.read_error = read_error
        self.puts = 0
        self.reads = []
        self.stats = 0
        self.collection = False
        self.unreadable = False

    def _ensure_parent_sync(self, remote_path):
        pass

    def _stat_sync(self, remote_path):
        self.stats += 1
        if self.puts == 0:
            return ArchiveRemoteStat(exists=False)
        return ArchiveRemoteStat(exists=self.stored is not None,
                                 size_bytes=len(self.stored) if self.stored is not None else None,
                                 etag='"fixture"', is_collection=self.collection)

    def _request_sync(self, method, absolute_path, *, body=None, headers=None):
        self.assert_method(method)
        self.puts += 1
        return 405, {}, b"fixture response"

    @staticmethod
    def assert_method(method):
        assert method == "PUT", "no extra PUT, DELETE or MOVE is permitted"

    def _get_bytes_sync(self, remote_path, max_bytes):
        self.reads.append(max_bytes)
        if self.read_error:
            raise self.read_error
        if self.unreadable:
            return None
        if self.stored is not None and len(self.stored) > max_bytes:
            raise WebDavArchiveError("WebDAV metadata object exceeds read limit")
        return self.stored


class MetadataPutResultTests(unittest.TestCase):
    def test_failed_status_accepts_only_complete_matching_remote_payload(self):
        payload = b'{"media":[]}'
        port = MetadataResultFixture(stored=payload)
        receipt = port._put_bytes_sync(payload, "fixture/renditions.json", "application/json")
        self.assertEqual(receipt.verification_method, "content")
        self.assertEqual(receipt.size_bytes, len(payload))
        self.assertEqual(receipt.etag, '"fixture"')
        self.assertEqual(port.puts, 1)
        self.assertEqual(port.reads, [len(payload)])
        self.assertEqual(port.stats, 2)

    def test_failed_status_rejects_absence_size_conflict_and_same_size_wrong_bytes(self):
        payload = b'{"media":[]}'
        for stored in (None, b"x", b"xxxxxxxxxxxx", b'{"media":{}}'):
            with self.subTest(stored=stored):
                port = MetadataResultFixture(stored=stored)
                with self.assertRaises(WebDavArchiveError) as caught:
                    port._put_bytes_sync(payload, "fixture/renditions.json", "application/json")
                self.assertEqual(caught.exception.status, 405)
                self.assertEqual(port.puts, 1)

    def test_unreadable_content_preserves_status_and_never_accepts_size_alone(self):
        payload = b'{"media":[]}'
        for error in (TimeoutError("fixture timeout"),
                      WebDavArchiveError("WebDAV metadata object exceeds read limit"), None):
            with self.subTest(error=type(error).__name__):
                port = MetadataResultFixture(stored=payload, read_error=error)
                port.unreadable = error is None
                with self.assertRaises(WebDavArchiveError) as caught:
                    port._put_bytes_sync(payload, "fixture/renditions.json", "application/json")
                self.assertEqual(caught.exception.status, 405)
                self.assertEqual(port.puts, 1)
                self.assertEqual(port.reads, [len(payload)])


    def test_collection_is_never_accepted_as_matching_metadata(self):
        payload = b'{"media":[]}'
        port = MetadataResultFixture(stored=payload)
        port.collection = True
        with self.assertRaises(WebDavArchiveError) as caught:
            port._put_bytes_sync(payload, "fixture/renditions.json", "application/json")
        self.assertEqual(caught.exception.status, 405)
        self.assertEqual(port.reads, [])
        self.assertEqual(port.puts, 1)
