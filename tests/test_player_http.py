from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp.test_utils import TestClient, TestServer

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.adapters.http.server import resolve_client
from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.application.playback import StartupRangeCache
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.auth import token_digest
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


class FakeDeleteClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failures: set[tuple[str, str]] = set()

    async def delete_location(self, package_path: str, remote_relpath: str) -> bool:
        self.calls.append((package_path, remote_relpath))
        return (package_path, remote_relpath) not in self.failures


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
        self.delete_client = FakeDeleteClient()
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.read_client),
            deleter=self.delete_client,
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

    async def test_feed_limits_background_range_prefetch_to_five_items(self) -> None:
        cookie = await self._login()
        self.server._deck.next_items = AsyncMock(return_value=[self.media_id] * 7)
        self.server._schedule_head_prefetch = AsyncMock()

        response = await self.client.get(
            "/api/v1/feed?limit=20&cache=1",
            cookies={"tgvio_player_session": cookie},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(len((await response.json())["items"]), 7)
        self.assertEqual(self.server._schedule_head_prefetch.await_count, 5)

    async def test_head_prefetch_is_limited_to_one_cache_chunk(self) -> None:
        cache = Mock(chunk_bytes=1024)
        self.server._range_cache = cache

        await self.server._schedule_head_prefetch(self.media_id, {"size_bytes": 4096})

        self.assertEqual(cache.prefetch_head.call_args.args[4], 1024)
        self.assertEqual(cache.prefetch_head.call_args.kwargs["whole_below"], 1024)

    async def test_delete_media_removes_every_registered_file_but_no_folder(self) -> None:
        duplicate = CatalogPackage(
            "duplicate-package", "TGVIO/2026-09-23/2", "c" * 64,
            '"manifest-2"', '"complete-2"',
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "duplicate-package", "nested/copy.mp4"),),
        )
        await self.repo.apply_package(duplicate)
        await self.repo.refresh_media_activity()
        cookie = await self._login()

        response = await self.client.delete(
            f"/api/v1/media/{self.media_id}",
            cookies={"tgvio_player_session": cookie},
        )

        self.assertEqual(response.status, 200)
        self.assertCountEqual(
            self.delete_client.calls,
            [
                ("TGVIO/2026-09-22/1", "video.mp4"),
                ("TGVIO/2026-09-23/2", "nested/copy.mp4"),
            ],
        )
        self.assertTrue(all(relpath.endswith(".mp4") for _, relpath in self.delete_client.calls))
        self.assertEqual((await response.json())["deleted_copies"], 2)
        self.assertIsNone(await self.repo.active_media_details(self.media_id))

    async def test_deleted_location_tombstone_prevents_catalog_resurrection(self) -> None:
        cookie = await self._login()
        response = await self.client.delete(
            f"/api/v1/media/{self.media_id}",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 200)

        package = CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, '"manifest"', '"complete"',
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()

        self.assertIsNone(await self.repo.active_media_details(self.media_id))

    async def test_delete_media_reports_partial_failure_and_keeps_remaining_copy(self) -> None:
        duplicate = CatalogPackage(
            "duplicate-package", "TGVIO/2026-09-23/2", "c" * 64,
            '"manifest-2"', '"complete-2"',
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "duplicate-package", "copy.mp4"),),
        )
        await self.repo.apply_package(duplicate)
        await self.repo.refresh_media_activity()
        self.delete_client.failures.add(("TGVIO/2026-09-23/2", "copy.mp4"))
        cookie = await self._login()

        response = await self.client.delete(
            f"/api/v1/media/{self.media_id}",
            cookies={"tgvio_player_session": cookie},
        )

        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["deleted_copies"], 1)
        self.assertEqual(body["failed_copies"], 1)
        self.assertFalse(body["removed"])
        self.assertIsNotNone(await self.repo.active_media_details(self.media_id))

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
        first_body = self.read_client.body
        first = asyncio.create_task(self.client.get(
            f"/api/v1/media/{self.media_id}/stream", cookies={"tgvio_player_session": cookie}
        ))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.client.get(
            f"/api/v1/media/{self.media_id}/stream", cookies={"tgvio_player_session": cookie}
        ))
        await asyncio.sleep(0.05)
        self.assertFalse(second.done(), "same-client playback should wait for the previous request to release")
        first_response = await first
        self.read_client.body = ClosableBody([b"x"])
        first_response.close()
        await asyncio.wait_for(first_body.closed.wait(), timeout=1)
        second_response = await asyncio.wait_for(second, timeout=1)
        self.assertEqual(second_response.status, 200)
        self.read_client.status = 503
        failed = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(failed.status, 502)
        self.assertTrue(self.read_client.body.closed.is_set())

    async def test_foreground_stream_waits_for_capacity_instead_of_returning_429(self) -> None:
        self.assertTrue(await self.server._acquire_stream("preload-client", preload=True))
        self.assertTrue(await self.server._acquire_stream("playback-client"))
        waiting = asyncio.create_task(self.server._acquire_stream("next-playback-client"))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done(), "foreground playback should wait briefly for a stream slot")

        await self.server._release_stream("preload-client", preload=True)
        self.assertTrue(await asyncio.wait_for(waiting, timeout=1))

        await self.server._release_stream("playback-client")
        await self.server._release_stream("next-playback-client")

    async def test_preload_preserves_one_global_slot_for_foreground_playback(self) -> None:
        self.assertTrue(await self.server._acquire_stream("playback-client"))
        diagnostics: dict[str, object] = {}
        acquired = await self.server._acquire_stream(
            "speculative-client", preload=True, diagnostics=diagnostics
        )
        self.assertFalse(acquired)
        self.assertEqual(diagnostics["reason"], "playback_capacity_reserved")
        await self.server._release_stream("playback-client")

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

    async def test_favorites_are_cursor_paged_with_stable_ties(self) -> None:
        cookie = await self._login()
        media_ids = [self.media_id, "c" * 64, "b" * 64]
        package = CatalogPackage(
            "favorite-page-package", "TGVIO/2026-09-22/2", "d" * 64,
            '"manifest-2"', '"complete-2"',
            tuple(CatalogMedia(media_id, "video", 4, "video/mp4", 1080, 1920, 2.0) for media_id in media_ids[1:]),
            tuple(CatalogLocation(media_id, "favorite-page-package", f"{media_id}.mp4") for media_id in media_ids[1:]),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        for media_id in media_ids:
            response = await self.client.put(
                f"/api/v1/media/{media_id}/favorite", cookies={"tgvio_player_session": cookie}
            )
            self.assertEqual(response.status, 200)
        # Use a shared timestamp to exercise the media ID tie-breaker.
        self.repo._require().execute(
            "UPDATE favorites SET created_at=123 WHERE token_digest=?", (token_digest(cookie),)
        )
        first = await self.client.get(
            "/api/v1/favorites?limit=1", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(first.status, 200)
        first_body = await first.json()
        self.assertTrue(first_body["has_more"])
        self.assertIsNotNone(first_body["next_cursor"])
        ids = [first_body["items"][0]["id"]]
        second = await self.client.get(
            "/api/v1/favorites?limit=1&cursor=" + first_body["next_cursor"],
            cookies={"tgvio_player_session": cookie},
        )
        second_body = await second.json()
        ids.extend(item["id"] for item in second_body["items"])
        third = await self.client.get(
            "/api/v1/favorites?limit=1&cursor=" + second_body["next_cursor"],
            cookies={"tgvio_player_session": cookie},
        )
        third_body = await third.json()
        ids.extend(item["id"] for item in third_body["items"])
        self.assertFalse(third_body["has_more"])
        self.assertEqual(ids, sorted(media_ids))
        self.assertNotIn("remote_path", str(first_body))
        self.assertEqual(
            (await self.client.get("/api/v1/favorites?cursor=bad", cookies={"tgvio_player_session": cookie})).status,
            400,
        )
        self.assertEqual((await self.client.get("/api/v1/favorites?limit=1")).status, 401)

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
        # Keep room for a foreground slot while the preload and active playback
        # run together. With two slots, the new admission rule correctly rejects
        # this preload to preserve that final slot.
        self.server._max_streams = 3
        self.server._max_preload = 1
        self.server._stream_slots = asyncio.BoundedSemaphore(3)
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

    async def test_public_pwa_assets_are_served_from_an_allowlist(self) -> None:
        static_dir = Path(self.tmp.name) / "pwa"
        static_dir.mkdir()
        names = (
            "site.webmanifest",
            "apple-touch-icon.png",
            "player-icon-192.png",
            "player-icon-512.png",
            "player-icon.svg",
        )
        for name in names:
            (static_dir / name).write_bytes(name.encode())
        server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.read_client),
            static_dir=static_dir,
        )
        client = TestClient(TestServer(server.application()))
        await client.start_server()
        try:
            for name in names:
                response = await client.get(f"/{name}")
                self.assertEqual(response.status, 200, name)
                self.assertEqual(await response.read(), name.encode())
            self.assertEqual((await client.get("/not-a-public-file.txt")).status, 404)
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


class VideoCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.short_id = "1" * 64
        self.long_id = "2" * 64
        package = CatalogPackage(
            "package",
            "TGVIO/2026-09-22/1",
            "b" * 64,
            '"manifest"',
            '"complete"',
            (
                CatalogMedia(self.short_id, "video", 1000, "video/mp4", 1080, 1920, 12.0, codec="h264"),
                CatalogMedia(self.long_id, "video", 5000, "video/mp4", 1080, 1920, 900.0, codec="h264"),
            ),
            (
                CatalogLocation(self.short_id, "package", "short.mp4", '"etag"'),
                CatalogLocation(self.long_id, "package", "long.mp4", '"etag"'),
            ),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()

        class DummyReader:
            async def open_range(self, remote_path, byte_range):
                raise AssertionError("streaming is not expected in this test")

        server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo, max_duration_seconds=300),
            DummyReader(),
            large_video_seconds=300,
        )
        self.client = TestClient(TestServer(server.application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def _login(self) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.assertEqual(response.status, 200)
        return response.cookies["tgvio_player_session"].value

    async def test_long_and_short_categories(self) -> None:
        cookie = await self._login()
        long_items = await self.client.get(
            "/api/v1/videos?category=long", cookies={"tgvio_player_session": cookie}
        )
        body = await long_items.json()
        self.assertEqual([item["id"] for item in body["items"]], [self.long_id])
        self.assertEqual(body["items"][0]["category"], "long")
        self.assertEqual(body["items"][0]["size_bytes"], 5000)
        short_items = await self.client.get(
            "/api/v1/videos?category=short", cookies={"tgvio_player_session": cookie}
        )
        body = await short_items.json()
        self.assertEqual([item["id"] for item in body["items"]], [self.short_id])
        self.assertEqual(body["items"][0]["category"], "short")

    async def test_cache_query_adds_prefetch_to_stream_url(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            "/api/v1/videos?category=long&cache=1", cookies={"tgvio_player_session": cookie}
        )
        item = (await response.json())["items"][0]
        self.assertTrue(item["stream_url"].endswith("?cache=1"))

    async def test_short_feed_excludes_large_videos(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            "/api/v1/feed?limit=20", cookies={"tgvio_player_session": cookie}
        )
        ids = [item["id"] for item in (await response.json())["items"]]
        self.assertEqual(ids, [self.short_id])

    @staticmethod
    def _group_id(name: str) -> str:
        return base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")

    async def test_media_and_group_pages_follow_archive_date_groups(self) -> None:
        sibling_id = "3" * 64
        sibling_package = CatalogPackage(
            "sibling-package",
            "TGVIO/2026-09-22/2",
            "c" * 64,
            '"manifest-2"',
            '"complete-2"',
            (
                CatalogMedia(self.short_id, "video", 1000, "video/mp4", 1080, 1920, 12.0),
                CatalogMedia(sibling_id, "video", 2000, "video/mp4", 1080, 1920, 20.0),
            ),
            (
                CatalogLocation(self.short_id, "sibling-package", "duplicate.mp4", '"etag-2"'),
                CatalogLocation(sibling_id, "sibling-package", "sibling.mp4", '"etag-2"'),
            ),
        )
        another_date_package = CatalogPackage(
            "another-date-package",
            "TGVIO/2026-09-23/1",
            "d" * 64,
            '"manifest-3"',
            '"complete-3"',
            (CatalogMedia(self.short_id, "video", 1000, "video/mp4", 1080, 1920, 12.0),),
            (CatalogLocation(self.short_id, "another-date-package", "same-video.mp4", '"etag-3"'),),
        )
        await self.repo.apply_package(sibling_package)
        await self.repo.apply_package(another_date_package)
        await self.repo.refresh_media_activity()
        cookie = await self._login()

        details = await self.client.get(
            f"/api/v1/media/{self.short_id}", cookies={"tgvio_player_session": cookie}
        )
        details_body = await details.json()
        self.assertEqual(
            {group["label"] for group in details_body["groups"]},
            {"2026-09-22", "2026-09-23"},
        )
        self.assertNotIn("remote_path", str(details_body))

        group_id = self._group_id("2026-09-22")
        first_page = await self.client.get(
            f"/api/v1/groups/{group_id}/videos?limit=2",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(first_page.status, 200)
        first_body = await first_page.json()
        self.assertEqual(first_body["group"], {"id": group_id, "label": "2026-09-22"})
        first_ids = [item["id"] for item in first_body["items"]]
        self.assertEqual(len(first_ids), 2)
        self.assertEqual(len(set(first_ids)), 2)
        self.assertTrue(first_body["has_more"])
        self.assertNotIn("remote_path", str(first_body))

        second_page = await self.client.get(
            f"/api/v1/groups/{group_id}/videos?limit=2&cursor={first_body['next_cursor']}",
            cookies={"tgvio_player_session": cookie},
        )
        second_body = await second_page.json()
        all_ids = first_ids + [item["id"] for item in second_body["items"]]
        self.assertEqual(set(all_ids), {self.short_id, self.long_id, sibling_id})
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertFalse(second_body["has_more"])
        self.assertEqual(
            {item["category"] for item in first_body["items"] + second_body["items"]},
            {"short", "long"},
        )

        another_date_id = self._group_id("2026-09-23")
        another_date = await self.client.get(
            f"/api/v1/groups/{another_date_id}/videos?limit=20",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual([item["id"] for item in (await another_date.json())["items"]], [self.short_id])

    async def test_group_feed_authentication_and_validation(self) -> None:
        group_id = self._group_id("2026-09-22")
        unauthenticated = await self.client.get(f"/api/v1/groups/{group_id}/videos")
        self.assertEqual(unauthenticated.status, 401)
        cookie = await self._login()
        unknown = await self.client.get(
            f"/api/v1/groups/{self._group_id('2026-09-24')}/videos",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(unknown.status, 404)
        malformed = await self.client.get(
            "/api/v1/groups/!!!/videos", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(malformed.status, 400)
        bad_cursor = await self.client.get(
            f"/api/v1/groups/{group_id}/videos?cursor=not-a-media-id",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(bad_cursor.status, 400)

    async def test_long_video_progress_is_shared_between_authenticated_sessions(self) -> None:
        first_cookie = await self._login()
        second_cookie = await self._login()
        saved = await self.client.put(
            f"/api/v1/media/{self.long_id}/progress",
            json={"position_seconds": 412.5},
            cookies={"tgvio_player_session": first_cookie},
        )
        self.assertEqual(saved.status, 200)
        listed = await self.client.get(
            "/api/v1/long-progress",
            cookies={"tgvio_player_session": second_cookie},
        )
        self.assertEqual(listed.status, 200)
        self.assertEqual(
            (await listed.json())["items"],
            [{"id": self.long_id, "position_seconds": 412.5}],
        )

    async def test_short_video_cannot_be_added_to_long_resume_progress(self) -> None:
        cookie = await self._login()
        response = await self.client.put(
            f"/api/v1/media/{self.short_id}/progress",
            json={"position_seconds": 4},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 404)

    async def test_invalid_category_is_rejected(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            "/api/v1/videos?category=medium", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(response.status, 400)


class RangeCacheHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.buffer = bytes((index * 7) % 251 for index in range(200_000))
        self.media_id = "9" * 64
        package = CatalogPackage(
            "package",
            "TGVIO/2026-09-22/1",
            "b" * 64,
            '"manifest"',
            '"complete"',
            (CatalogMedia(self.media_id, "video", len(self.buffer), "video/mp4", 1080, 1920, 20.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()

        class BufferReader:
            def __init__(self, buffer: bytes) -> None:
                self.buffer = buffer
                self.calls: list[tuple[int, int]] = []

            async def open_range(self, package: str, relpath: str, byte_range: ByteRange):
                self.calls.append((byte_range.start, byte_range.end))
                payload = self.buffer[byte_range.start : byte_range.end + 1]

                class Body:
                    def __init__(self, data: bytes) -> None:
                        self.data = data
                        self.done = False

                    def __aiter__(self):
                        return self

                    async def __anext__(self) -> bytes:
                        if self.done:
                            raise StopAsyncIteration
                        self.done = True
                        return self.data

                    async def aclose(self) -> None:
                        return None

                class Response:
                    status = 206
                    content_length = len(payload)
                    body = Body(payload)

                return Response()

        from tgvio_player.application.range_cache import MediaRangeCache
        from tgvio_player.infrastructure.range_store import RangeStore

        self.reader = BufferReader(self.buffer)
        cache = MediaRangeCache(
            RangeStore(
                Path(self.tmp.name) / "cache", chunk_bytes=64 * 1024, max_bytes=8 * 64 * 1024
            ),
            self.reader,
        )
        cache.open()
        self.cache = cache
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            self.reader,
            range_cache=cache,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.cache.shutdown()
        await self.repo.close()
        self.tmp.cleanup()

    async def _login(self) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.assertEqual(response.status, 200)
        return response.cookies["tgvio_player_session"].value

    async def test_first_range_fetches_chunks_and_second_reuses_cache(self) -> None:
        cookie = await self._login()
        stream = f"/api/v1/media/{self.media_id}/stream"
        first = await self.client.get(
            stream, headers={"Range": "bytes=131072-196607"}, cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(first.status, 206)
        self.assertEqual(await first.read(), self.buffer[131072:196608])
        for _ in range(20):
            if self.cache.has_chunk(str(self.media_id), 2):
                break
            await asyncio.sleep(0)
        self.assertEqual(self.reader.calls, [(0, 199999)])
        second = await self.client.get(
            stream, headers={"Range": "bytes=140000-150000"}, cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(await second.read(), self.buffer[140000:150001])
        # Chunk 2 is cached, so no further upstream read happens.
        self.assertEqual(self.reader.calls, [(0, 199999)])

    async def test_cache_stats_are_authenticated_and_report_stream_counters(self) -> None:
        denied = await self.client.get("/api/v1/cache-stats")
        self.assertEqual(denied.status, 401)

        cookie = await self._login()
        allowed = await self.client.get(
            "/api/v1/cache-stats", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(allowed.status, 200)
        stats = await allowed.json()
        self.assertIn("bytes", stats)
        self.assertIn("disk_cache_bytes_served", stats)
        self.assertIn("inflight_bytes_served", stats)
        self.assertIn("upstream_bytes", stats)
        self.assertIn("prime_wait_ms_avg", stats)


if __name__ == "__main__":
    unittest.main()
