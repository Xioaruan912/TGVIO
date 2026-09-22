from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aiohttp.test_utils import TestClient, TestServer

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.adapters.http.server import resolve_client
from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.application.playback import StartupRangeCache
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_read import ReadOnlyWebDavAdapter, WebDavRangeResponse


class ClosableBody:
    def __init__(self, chunks: list[bytes], *, wait: asyncio.Event | None = None) -> None:
        self._chunks = chunks
        self._wait = wait
        self.closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._wait is not None:
            await self._wait.wait()
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.closed.set()


class FakeReadClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ByteRange | None]] = []
        self.body = ClosableBody([b"abcd"])
        self.status = 206

    async def open_range(self, remote_path: str, byte_range: ByteRange | None) -> WebDavRangeResponse:
        self.calls.append((remote_path, byte_range))
        body = self.body
        if byte_range is None:
            return WebDavRangeResponse(self.status, "video/mp4", 4, None, '"etag"', body)
        return WebDavRangeResponse(
            self.status,
            "video/mp4",
            byte_range.length,
            f"bytes {byte_range.start}-{byte_range.end}/4",
            '"etag"',
            body,
        )


class PlayerHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.media_id = "a" * 64
        package = CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, '"manifest"', '"complete"',
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        self.read_client = FakeReadClient()
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.read_client),
            max_streams=2,
            max_streams_per_client=1,
            startup_cache=StartupRangeCache(max_entries=2, max_bytes=8),
            startup_range_bytes=4,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def _login(self) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.assertEqual(response.status, 200)
        return response.cookies["tgvio_player_session"].value

    async def test_auth_feed_and_favorite_never_expose_location(self) -> None:
        self.assertEqual((await self.client.get("/api/v1/feed")).status, 401)
        self.assertEqual((await self.client.post("/api/v1/auth/login", json={"secret": "wrong"})).status, 401)
        cookie = await self._login()
        self.assertEqual((await self.client.get(
            "/api/v1/feed?token=leak", cookies={"tgvio_player_session": cookie}
        )).status, 400)
        response = await self.client.get("/api/v1/feed?limit=1", cookies={"tgvio_player_session": cookie})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["items"][0]["id"], self.media_id)
        self.assertFalse(body["items"][0]["favorite"])
        self.assertNotIn("remote_path", str(body))
        favorite = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(favorite.status, 200)
        details = await self.client.get(
            f"/api/v1/media/{self.media_id}", cookies={"tgvio_player_session": cookie}
        )
        self.assertTrue((await details.json())["favorite"])
        self.assertEqual((await self.client.delete(
            f"/api/v1/media/{self.media_id}/favorite", cookies={"tgvio_player_session": cookie}
        )).status, 200)

    async def test_range_headers_security_and_invalid_range(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=1-3"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b"abc")
        self.assertEqual(self.read_client.calls, [("TGVIO/2026-09-22/1/video.mp4", ByteRange(1, 3))])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertTrue(self.read_client.body.closed.is_set())
        invalid = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=-2"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(invalid.status, 416)

    async def test_stream_capacity_releases_on_client_cancellation(self) -> None:
        cookie = await self._login()
        wait = asyncio.Event()
        self.read_client.body = ClosableBody([b"later"], wait=wait)
        first = asyncio.create_task(self.client.get(
            f"/api/v1/media/{self.media_id}/stream", cookies={"tgvio_player_session": cookie}
        ))
        await asyncio.sleep(0)
        second = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(second.status, 429)
        first_response = await first
        first_response.close()
        await asyncio.wait_for(self.read_client.body.closed.wait(), timeout=1)
        self.read_client.body = ClosableBody([b"x"])
        self.read_client.status = 503
        failed = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(failed.status, 502)
        self.assertTrue(self.read_client.body.closed.is_set())

    async def test_startup_range_is_cached_with_catalog_etag_and_range_headers(self) -> None:
        cookie = await self._login()
        first = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=0-3"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(first.status, 206)
        self.assertEqual(await first.read(), b"abcd")
        self.assertEqual(first.headers["Content-Range"], "bytes 0-3/4")
        self.assertEqual(first.headers["Content-Length"], "4")
        self.assertEqual(first.headers["ETag"], '"etag"')
        self.assertTrue(self.read_client.body.closed.is_set())

        self.read_client.body = ClosableBody([b"wrong"])
        second = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=0-3"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(await second.read(), b"abcd")
        self.assertEqual(len(self.read_client.calls), 1)
        self.assertFalse(self.read_client.body.closed.is_set())

    async def test_open_ended_range_bypasses_startup_cache(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=0-"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b"abcd")
        self.assertEqual(self.read_client.calls[0][1], ByteRange(0, 3))

    async def test_logout_and_cross_origin_mutations_are_rejected(self) -> None:
        cookie = await self._login()
        forbidden = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            headers={"Origin": "https://attacker.invalid"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(forbidden.status, 403)
        logout = await self.client.post("/api/v1/auth/logout", cookies={"tgvio_player_session": cookie})
        self.assertEqual(logout.status, 200)
        self.assertEqual((await self.client.get(
            f"/api/v1/media/{self.media_id}", cookies={"tgvio_player_session": cookie}
        )).status, 401)

    async def test_proxy_forwarded_origin_is_accepted_behind_tls_termination(self) -> None:
        cookie = await self._login()
        headers = {
            "Origin": "https://csdn.im",
            "X-Forwarded-Host": "csdn.im",
            "X-Forwarded-Proto": "https",
        }
        ok = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            headers=headers,
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(ok.status, 200)
        # The mutation is idempotent and the favorites listing stays path-free.
        again = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            headers=headers,
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(again.status, 200)
        listed = await self.client.get(
            "/api/v1/favorites", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(listed.status, 200)
        body = await listed.json()
        self.assertEqual([item["id"] for item in body["items"]], [self.media_id])
        self.assertNotIn("remote", str(body))
        removed = await self.client.delete(
            f"/api/v1/media/{self.media_id}/favorite",
            headers=headers,
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(removed.status, 200)
        empty = await self.client.get(
            "/api/v1/favorites", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual((await empty.json())["items"], [])
        # A hostile Origin is still rejected even if a forwarded host is present.
        hostile = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            headers={"Origin": "https://attacker.invalid", "X-Forwarded-Host": "csdn.im"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(hostile.status, 403)
        self.assertEqual(
            (await self.client.get("/api/v1/favorites")).status, 401
        )

    async def test_failed_logins_are_rate_limited_and_success_clears_failures(self) -> None:
        for _ in range(4):
            response = await self.client.post("/api/v1/auth/login", json={"secret": "wrong"})
            self.assertEqual(response.status, 401)
        self.assertEqual((await self.client.post(
            "/api/v1/auth/login", json={"secret": "s" * 32}
        )).status, 200)
        for _ in range(5):
            response = await self.client.post("/api/v1/auth/login", json={"secret": "wrong"})
            self.assertEqual(response.status, 401)
        blocked = await self.client.post("/api/v1/auth/login", json={"secret": "wrong"})
        self.assertEqual(blocked.status, 429)
        self.assertIn("Retry-After", blocked.headers)


    async def test_preload_requests_do_not_consume_playback_budget(self) -> None:
        cookie = await self._login()
        stream = f"/api/v1/media/{self.media_id}/stream"
        hold = asyncio.Event()
        self.read_client.body = ClosableBody([b"hold"], wait=hold)
        first = asyncio.create_task(self.client.get(
            stream, headers={"Range": "bytes=2-3"}, cookies={"tgvio_player_session": cookie}
        ))
        await asyncio.sleep(0)
        blocked = await self.client.get(
            stream, headers={"Range": "bytes=2-3"}, cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(blocked.status, 429)

        preload_hold = asyncio.Event()
        self.read_client.body = ClosableBody([b"warm"], wait=preload_hold)
        preload = asyncio.create_task(self.client.get(
            stream,
            headers={"Range": "bytes=2-3", "X-TGVIO-Preload": "1"},
            cookies={"tgvio_player_session": cookie},
        ))
        await asyncio.sleep(0)
        second_preload = await self.client.get(
            stream,
            headers={"Range": "bytes=2-3", "X-TGVIO-Preload": "1"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(second_preload.status, 429)

        preload_hold.set()
        preload_response = await preload
        self.assertEqual(preload_response.status, 206)
        preload_response.close()
        hold.set()
        first_response = await first
        first_response.close()

    async def test_cache_headers_separate_api_and_assets(self) -> None:
        web_dir = Path(self.tmp.name) / "web"
        (web_dir / "assets").mkdir(parents=True)
        (web_dir / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
        (web_dir / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.read_client),
            static_dir=web_dir,
        )
        client = TestClient(TestServer(server.application()))
        await client.start_server()
        try:
            health = await client.get("/healthz")
            self.assertEqual(health.headers["Cache-Control"], "no-store")
            root = await client.get("/")
            self.assertEqual(root.headers["Cache-Control"], "no-cache")
            asset = await client.get("/assets/app.js")
            self.assertEqual(
                asset.headers["Cache-Control"], "public, max-age=31536000, immutable"
            )
            feed = await client.get("/api/v1/feed")
            self.assertEqual(feed.status, 401)
            self.assertEqual(feed.headers["Cache-Control"], "no-store")
        finally:
            await client.close()


class ClientIdentityTests(unittest.TestCase):
    def test_resolve_client_behind_trusted_proxy(self) -> None:
        class FakeRequest:
            def __init__(self, remote: str | None, headers: dict[str, str]) -> None:
                self.remote = remote
                self.headers = headers

        self.assertEqual(
            resolve_client(FakeRequest("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})),
            "203.0.113.9",
        )
        self.assertEqual(
            resolve_client(FakeRequest("172.20.0.1", {"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})),
            "198.51.100.7",
        )
        self.assertEqual(
            resolve_client(FakeRequest("127.0.0.1", {"X-Real-IP": "198.51.100.9"})),
            "198.51.100.9",
        )
        self.assertEqual(resolve_client(FakeRequest("172.20.0.1", {})), "172.20.0.1")
        self.assertEqual(resolve_client(FakeRequest(None, {})), "unknown")


if __name__ == "__main__":
    unittest.main()
