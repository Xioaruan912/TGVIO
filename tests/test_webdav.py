import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from src import webdav


class _DavState:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.put_calls = 0
        self.propfind_calls = 0
        self.delete_calls = 0
        self.put_status = 201
        self.put_delay = 0.0
        self.store_put = True


class _DavHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _DavState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args) -> None:
        return

    def _path(self) -> str:
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.scheme:
            parsed = urllib.parse.urlsplit(f"http://local{self.path}")
        return urllib.parse.unquote(parsed.path)

    def _respond(self, status: int, body: bytes = b"") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_MKCOL(self) -> None:
        self._respond(201)

    def do_PUT(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(size)
        self.state.put_calls += 1
        if self.state.store_put:
            self.state.files[self._path()] = payload
        if self.state.put_delay:
            time.sleep(self.state.put_delay)
        self._respond(self.state.put_status)

    def do_PROPFIND(self) -> None:
        self.state.propfind_calls += 1
        payload = self.state.files.get(self._path())
        if payload is None:
            self._respond(404)
            return
        body = (
            '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
            "<D:response><D:propstat><D:prop>"
            f"<D:getcontentlength>{len(payload)}</D:getcontentlength>"
            "</D:prop></D:propstat></D:response></D:multistatus>"
        ).encode()
        self._respond(207, body)

    def do_DELETE(self) -> None:
        self.state.delete_calls += 1
        existed = self.state.files.pop(self._path(), None) is not None
        self._respond(204 if existed else 404)


class WebDavProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _DavState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DavHandler)
        self.server.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}/dav"
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.tempdir.cleanup()

    def make_file(self, content: bytes = b"payload") -> str:
        path = os.path.join(self.tempdir.name, "local.mp4")
        with open(path, "wb") as media_file:
            media_file.write(content)
        return path

    def test_upload_remote_size_and_delete_round_trip(self) -> None:
        path = self.make_file(b"round-trip")
        with patch.object(webdav, "_VERIFY_ATTEMPTS", 2), patch.object(
            webdav, "_VERIFY_INTERVAL", 0
        ):
            uploaded = webdav.upload_file(
                self.base_url,
                "backup/1",
                path,
                "user",
                "pass",
                retries=0,
                remote_name="abc12345.mp4",
            )

        self.assertTrue(uploaded)
        self.assertEqual(self.state.put_calls, 1)
        self.assertEqual(
            webdav.remote_file_size(
                self.base_url,
                "backup/1",
                "abc12345.mp4",
                "user",
                "pass",
            ),
            len(b"round-trip"),
        )
        self.assertTrue(
            webdav.delete_remote(
                self.base_url,
                "backup/1",
                "abc12345.mp4",
                "user",
                "pass",
                retries=0,
            )
        )
        self.assertIsNone(
            webdav.remote_file_size(
                self.base_url,
                "backup/1",
                "abc12345.mp4",
                "user",
                "pass",
            )
        )

    def test_locked_put_is_success_when_propfind_confirms_complete_file(self) -> None:
        self.state.put_status = 423
        path = self.make_file(b"stored-before-locked-response")
        with patch.object(webdav, "_VERIFY_ATTEMPTS", 2), patch.object(
            webdav, "_VERIFY_INTERVAL", 0
        ):
            uploaded = webdav.upload_file(
                self.base_url,
                "backup/2",
                path,
                "user",
                "pass",
                retries=0,
                remote_name="locked.mp4",
            )

        self.assertTrue(uploaded)
        self.assertEqual(self.state.put_calls, 1)
        self.assertGreaterEqual(self.state.propfind_calls, 1)

    def test_put_response_timeout_falls_back_to_propfind_without_reupload(self) -> None:
        self.state.put_delay = 0.08
        path = self.make_file(b"slow-response")
        with patch.object(webdav, "_RESP_TIMEOUT", 0.01), patch.object(
            webdav, "_VERIFY_ATTEMPTS", 5
        ), patch.object(webdav, "_VERIFY_INTERVAL", 0.01):
            uploaded = webdav.upload_file(
                self.base_url,
                "backup/3",
                path,
                "user",
                "pass",
                retries=0,
                remote_name="slow.mp4",
            )

        self.assertTrue(uploaded)
        self.assertEqual(self.state.put_calls, 1)
        self.assertGreaterEqual(self.state.propfind_calls, 1)

    def test_failed_put_without_remote_file_is_not_reported_as_success(self) -> None:
        self.state.put_status = 500
        self.state.store_put = False
        path = self.make_file(b"missing")
        with patch.object(webdav, "_VERIFY_ATTEMPTS", 1), patch.object(
            webdav, "_VERIFY_INTERVAL", 0
        ):
            uploaded = webdav.upload_file(
                self.base_url,
                "backup/4",
                path,
                "user",
                "pass",
                retries=0,
                remote_name="missing.mp4",
            )

        self.assertFalse(uploaded)
        self.assertEqual(self.state.put_calls, 1)


if __name__ == "__main__":
    unittest.main()
