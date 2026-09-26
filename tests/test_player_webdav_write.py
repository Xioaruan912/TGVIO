from __future__ import annotations

import json
import unittest

from tgvio_player.infrastructure.webdav_aiohttp import PublicOnlyResolver
from tgvio_player.infrastructure.webdav_write import (
    AioHttpWebDavWriteClient,
    WebDavWriteError,
    safe_storage_relpath,
    validate_webdav_endpoint,
)


class FakeResponse:
    def __init__(
        self, status: int, *, headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.closed = False
        self.content = FakeContent(chunks)
        self.body = body

    async def read(self) -> bytes:
        return self.body

    def release(self) -> None:
        self.closed = True


class FakeContent:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk


class FakeSession:
    def __init__(self, status: int = 201, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.payload = bytearray()
        self.response_chunks: tuple[bytes, ...] = ()

    async def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        data = kwargs.get("data")
        if hasattr(data, "__aiter__"):
            async for chunk in data:  # type: ignore[union-attr]
                self.payload.extend(chunk)
        return FakeResponse(self.status, headers=self.headers, chunks=self.response_chunks)


class FakeResolver:
    def __init__(self, ip: str) -> None:
        self.ip = ip

    async def resolve(self, host: str, port: int = 0, family: int = 0):
        return [{"hostname": host, "host": self.ip, "port": port, "family": family, "proto": 0, "flags": 0}]

    async def close(self) -> None:
        return None


class FakeApiSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class WebDavWriteSafetyTests(unittest.TestCase):
    def test_endpoint_requires_https_without_credentials_or_private_ip(self) -> None:
        for value in (
            "http://dav.example.test", "https://user:pass@dav.example.test",
            "https://127.0.0.1", "https://169.254.2.3", "https://10.0.0.1",
            "https://[::1]", "https://dav.example.test/path?query=1",
            "https://dav.example.test/a/../b", "https://dav.example.test//dav",
            "https://dav.example.test/%2e%2e/dav",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_webdav_endpoint(value)
        self.assertEqual(validate_webdav_endpoint("https://dav.example.test"), "https://dav.example.test")
        self.assertEqual(
            validate_webdav_endpoint("https://dav.example.test/dav/"),
            "https://dav.example.test/dav",
        )

    def test_resolver_rejects_private_dns_answer(self) -> None:
        resolver = PublicOnlyResolver(delegate=FakeResolver("192.168.1.10"))

        async def check() -> None:
            with self.assertRaises(OSError):
                await resolver.resolve("public-looking.example", 443)

        import asyncio
        asyncio.run(check())

    def test_remote_paths_reject_traversal_absolute_and_controls(self) -> None:
        self.assertEqual(safe_storage_relpath("收藏/video one.mp4"), "收藏/video one.mp4")
        for value in ("/absolute", "../escape", "safe/../../escape", "a\\b", "a\x00b", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                safe_storage_relpath(value)


class WebDavWriteTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_openlist_copy_uses_json_api_without_streaming_file_bytes(self) -> None:
        api = FakeApiSession([
            FakeResponse(200, body=json.dumps({
                "code": 200, "data": {"token": "token-value"},
            }).encode()),
            FakeResponse(200, body=b'{"code":200,"message":"success","data":null}'),
        ])
        client = AioHttpWebDavWriteClient(
            "https://dav.example.test/dav", "u", "p",
            session=FakeSession(), api_session=api,
        )

        await client.copy(
            "archive/2026/1/source.mp4", "player/Favorites/media/source.mp4",
        )

        self.assertEqual([call[1] for call in api.calls], [
            "https://dav.example.test/api/auth/login",
            "https://dav.example.test/api/fs/copy",
        ])
        self.assertEqual(api.calls[1][2]["json"], {
            "src_dir": "/archive/2026/1",
            "dst_dir": "/player/Favorites/media",
            "names": ["source.mp4"],
        })

    async def test_configured_dav_path_prefixes_all_webdav_requests(self) -> None:
        session = FakeSession(201)
        client = AioHttpWebDavWriteClient("https://dav.example.test/dav", "u", "p", session=session)
        await client.ensure_directory("root/favorites")
        self.assertEqual(
            [call[1] for call in session.calls],
            [
                "https://dav.example.test/dav/root",
                "https://dav.example.test/dav/root/favorites",
            ],
        )

    async def test_mkcol_accepts_created_and_already_exists(self) -> None:
        for status in (201, 405):
            session = FakeSession(status)
            client = AioHttpWebDavWriteClient("https://dav.example.test", "u", "p", session=session)
            await client.ensure_directory("root/favorites")
            self.assertEqual([call[0] for call in session.calls], ["MKCOL", "MKCOL"])

    async def test_put_streams_declared_size_and_returns_safe_receipt(self) -> None:
        session = FakeSession(201, {"ETag": '"stored"'})
        client = AioHttpWebDavWriteClient("https://dav.example.test", "u", "p", session=session)

        async def chunks():
            yield b"first"
            yield b"second"

        receipt = await client.put_stream("root/file.mp4", chunks(), size_bytes=11, content_type="video/mp4")
        self.assertEqual(bytes(session.payload), b"firstsecond")
        self.assertEqual(session.calls[0][2]["headers"], {"Content-Length": "11", "Content-Type": "video/mp4"})
        self.assertEqual(receipt.status_code, 201)
        self.assertEqual(receipt.size_bytes, 11)
        self.assertEqual(receipt.etag, '"stored"')

    async def test_put_rejects_stream_size_mismatch(self) -> None:
        session = FakeSession(201)
        client = AioHttpWebDavWriteClient("https://dav.example.test", "u", "p", session=session)

        async def chunks():
            yield b"short"

        with self.assertRaises(WebDavWriteError):
            await client.put_stream("file.mp4", chunks(), size_bytes=10, content_type="video/mp4")

    async def test_delete_404_is_idempotent_and_statuses_are_classified(self) -> None:
        gone = AioHttpWebDavWriteClient("https://dav.example.test", "u", "p", session=FakeSession(404))
        receipt = await gone.delete("root/file.mp4")
        self.assertTrue(receipt.deleted)
        self.assertEqual(receipt.status_code, 404)

        for status, category in ((401, "unauthorized"), (429, "rate_limited"), (503, "server_error")):
            client = AioHttpWebDavWriteClient("https://dav.example.test", "u", "p", session=FakeSession(status))
            with self.subTest(status=status), self.assertRaises(WebDavWriteError) as raised:
                await client.ensure_directory("root")
            self.assertEqual(raised.exception.category, category)
            self.assertEqual(raised.exception.status_code, status)

    async def test_redirects_are_rejected_and_unsupported_move_is_classified(self) -> None:
        redirected = AioHttpWebDavWriteClient("https://dav.example.test", "u", "p", session=FakeSession(302))
        with self.assertRaises(WebDavWriteError) as raised:
            await redirected.stat("root/file")
        self.assertEqual(raised.exception.category, "unsafe_redirect")

        unsupported = AioHttpWebDavWriteClient("https://dav.example.test", "u", "p", session=FakeSession(405))
        with self.assertRaises(WebDavWriteError) as raised:
            await unsupported.move("root/temp", "root/manifest", overwrite=True)
        self.assertEqual(raised.exception.category, "move_unsupported")

    async def test_get_bytes_is_bounded_and_streaming_get_releases_response(self) -> None:
        session = FakeSession(200, {"Content-Length": "4", "Content-Type": "video/mp4"})
        session.response_chunks = (b"ab", b"cd")
        client = AioHttpWebDavWriteClient("https://dav.example.test", "u", "p", session=session)
        self.assertEqual(await client.get_bytes("root/config", max_bytes=4), b"abcd")

        session.response_chunks = (b"ab", b"cd")
        size, mime, body = await client.open_stream("root/video.mp4")
        self.assertEqual((size, mime), (4, "video/mp4"))
        self.assertEqual(b"".join([chunk async for chunk in body]), b"abcd")

        session.response_chunks = (b"abc", b"def")
        with self.assertRaises(WebDavWriteError) as raised:
            await client.get_bytes("root/config", max_bytes=4)
        self.assertEqual(raised.exception.category, "response_too_large")

    async def test_get_missing_is_idempotent_but_open_stream_requires_existing_file(self) -> None:
        client = AioHttpWebDavWriteClient(
            "https://dav.example.test", "u", "p", session=FakeSession(404),
        )
        self.assertIsNone(await client.get_bytes("root/missing", max_bytes=20))
        with self.assertRaises(WebDavWriteError) as raised:
            await client.open_stream("root/missing")
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
