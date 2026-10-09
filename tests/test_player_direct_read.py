"""Direct reads: OpenList links, reused CDN connections, and a WebDAV safety net."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.application import stream_trace
from tgvio_player.application.archive_read import ArchiveReadRouter, ReadModeService, ReadModeUnavailable
from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.infrastructure.openlist_direct import OpenListDirectReader, OpenListDirectSettings
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse

PAYLOAD = bytes(range(256)) * 64  # 16 KiB
UA = "TGVIO-Player-Test/1"


class FakeArchive:
    """A loopback stand-in for OpenList's API and the CDN behind its links."""

    def __init__(self) -> None:
        self.logins = 0
        self.link_requests = 0
        self.cdn_peers: list[int] = []
        self.refuse_next_cdn = 0
        self.reject_next_token = 0
        self.link_lifetime = 3600
        self.link_ua: dict[str, str] = {}
        self.base = ""

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/auth/login", self.login)
        app.router.add_post("/api/fs/get", self.fs_get)
        app.router.add_get("/cdn/{key}", self.cdn)
        return app

    async def login(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.logins += 1
        if body != {"username": "reader", "password": "secret"}:
            return web.json_response({"code": 400, "data": None})
        return web.json_response({"code": 200, "data": {"token": f"token-{self.logins}"}})

    async def fs_get(self, request: web.Request) -> web.Response:
        if not request.headers.get("Authorization", "").startswith("token-"):
            return web.json_response({"code": 401, "data": None})
        if self.reject_next_token:
            self.reject_next_token -= 1
            return web.json_response({"code": 401, "data": None})
        body = await request.json()
        self.link_requests += 1
        key = f"k{self.link_requests}"
        # The real CDN binds a link to the User-Agent it was requested with.
        self.link_ua[key] = request.headers.get("User-Agent", "")
        expires = int(time.time()) + self.link_lifetime
        return web.json_response({"code": 200, "data": {
            "raw_url": f"{self.base}/cdn/{key}?t={expires}&path={body['path']}", "size": len(PAYLOAD),
        }})

    async def cdn(self, request: web.Request) -> web.StreamResponse:
        key = request.match_info["key"]
        if request.headers.get("User-Agent", "") != self.link_ua.get(key):
            return web.Response(status=403)
        if self.refuse_next_cdn:
            self.refuse_next_cdn -= 1
            return web.Response(status=403)
        self.cdn_peers.append(request.transport.get_extra_info("peername")[1])
        start, end = (int(x) for x in request.headers["Range"].removeprefix("bytes=").split("-"))
        data = PAYLOAD[start:end + 1]
        return web.Response(status=206, body=data, headers={
            "Content-Range": f"bytes {start}-{end}/{len(PAYLOAD)}", "Content-Type": "video/mp4",
        })


async def read_all(response: WebDavRangeResponse) -> bytes:
    return b"".join([chunk async for chunk in response.body])


class OpenListDirectReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fake = FakeArchive()
        self.server = TestServer(self.fake.app())
        await self.server.start_server()
        self.fake.base = str(self.server.make_url("")).rstrip("/")
        self.now = time.time()
        self.reader = OpenListDirectReader(
            OpenListDirectSettings(self.fake.base, "reader", "secret", user_agent=UA),
            clock=lambda: self.now,
        )

    async def asyncTearDown(self) -> None:
        await self.reader.close()
        await self.server.close()

    async def test_one_link_and_one_reused_connection_serve_many_ranges(self) -> None:
        first = await self.reader.open_range("archive/pkg", "video.mp4", ByteRange(0, 1023))
        self.assertEqual(first.status, 206)
        self.assertEqual(await read_all(first), PAYLOAD[:1024])
        second = await self.reader.open_range("archive/pkg", "video.mp4", ByteRange(8192, 9215))
        self.assertEqual(await read_all(second), PAYLOAD[8192:9216])
        self.assertEqual(self.fake.link_requests, 1, "the link is cached, not fetched per range")
        self.assertEqual(len(set(self.fake.cdn_peers)), 1, "both ranges travel on one kept-alive connection")
        self.assertEqual(self.reader.stats()["link_hits"], 1)

    async def test_a_refused_link_is_fetched_again_once(self) -> None:
        await read_all(await self.reader.open_range("archive/pkg", "video.mp4", ByteRange(0, 9)))
        self.fake.refuse_next_cdn = 1
        response = await self.reader.open_range("archive/pkg", "video.mp4", ByteRange(10, 19))
        self.assertEqual(await read_all(response), PAYLOAD[10:20])
        self.assertEqual(self.fake.link_requests, 2)
        self.assertEqual(self.reader.stats()["link_refreshes"], 1)

    async def test_a_link_is_replaced_before_its_stamped_expiry(self) -> None:
        self.fake.link_lifetime = 600
        await read_all(await self.reader.open_range("archive/pkg", "video.mp4", ByteRange(0, 9)))
        self.now += 600 - 60  # inside the two-minute safety margin
        await read_all(await self.reader.open_range("archive/pkg", "video.mp4", ByteRange(0, 9)))
        self.assertEqual(self.fake.link_requests, 2)

    async def test_an_expired_session_logs_in_again(self) -> None:
        await read_all(await self.reader.open_range("archive/pkg", "a.mp4", ByteRange(0, 9)))
        self.fake.reject_next_token = 1
        await read_all(await self.reader.open_range("archive/pkg", "b.mp4", ByteRange(0, 9)))
        self.assertEqual(self.fake.logins, 2)

    async def test_the_cdn_sees_the_user_agent_the_link_was_issued_for(self) -> None:
        response = await self.reader.open_range("archive/pkg", "video.mp4", ByteRange(0, 9))
        self.assertEqual(response.status, 206)
        self.assertEqual(self.fake.link_ua["k1"], UA)

    async def test_paths_outside_the_catalog_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            await self.reader.open_range("archive/pkg", "../escape.mp4", ByteRange(0, 9))
        self.assertEqual(self.fake.link_requests, 0)


class StubReader:
    def __init__(self, status: int = 206, error: Exception | None = None) -> None:
        self.status, self.error, self.calls = status, error, 0

    async def open_range(self, package, relpath, byte_range):
        self.calls += 1
        if self.error is not None:
            raise self.error

        async def body():
            yield b"x"

        return WebDavRangeResponse(self.status, "video/mp4", 1, None, None, body())


class ArchiveReadRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_webdav_mode_never_touches_the_direct_reader(self) -> None:
        webdav, direct = StubReader(), StubReader()
        router = ArchiveReadRouter(webdav, direct)
        await router.open_range("p", "v.mp4", ByteRange(0, 0))
        self.assertEqual((webdav.calls, direct.calls), (1, 0))

    async def test_a_failing_direct_read_falls_back_to_webdav(self) -> None:
        for direct in (StubReader(error=RuntimeError("cdn down")), StubReader(status=502)):
            webdav = StubReader()
            router = ArchiveReadRouter(webdav, direct, mode="direct")
            response = await router.open_range("p", "v.mp4", ByteRange(0, 0))
            self.assertEqual(response.status, 206)
            self.assertEqual((direct.calls, webdav.calls), (1, 1))
            self.assertEqual(router.stats()["direct_fallbacks"], 1)

    async def test_a_traced_request_learns_which_route_served_it(self) -> None:
        cases = (("webdav", StubReader()), ("direct", StubReader()), ("direct_fallback", StubReader(status=502)))
        for expected, direct in cases:
            router = ArchiveReadRouter(StubReader(), direct, mode="webdav" if expected == "webdav" else "direct")
            trace, token = stream_trace.begin()
            try:
                await router.open_range("p", "v.mp4", ByteRange(0, 0))
            finally:
                stream_trace.end(token)
            self.assertEqual(trace.via, expected)

    async def test_direct_mode_needs_a_direct_reader(self) -> None:
        router = ArchiveReadRouter(StubReader(), None, mode="direct")
        self.assertEqual(router.mode, "webdav")
        with self.assertRaises(ReadModeUnavailable):
            router.set_mode("direct")


class ReadModePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_the_choice_survives_a_restart_and_defaults_to_webdav(self) -> None:
        self.assertEqual(await self.repo.get_read_mode(), "webdav")
        service = ReadModeService(self.repo, ArchiveReadRouter(StubReader(), StubReader()))
        await service.choose("direct")
        restarted = ReadModeService(self.repo, ArchiveReadRouter(StubReader(), StubReader()))
        self.assertEqual(await restarted.load(), "direct")

    async def test_a_stored_direct_choice_without_a_direct_reader_loads_as_webdav(self) -> None:
        await self.repo.set_read_mode("direct")
        service = ReadModeService(self.repo, ArchiveReadRouter(StubReader(), None))
        self.assertEqual(await service.load(), "webdav")


class ReadModeHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.direct = StubReader()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def start(self, direct: object | None) -> None:
        router = ArchiveReadRouter(StubReader(), direct)
        self.service = ReadModeService(self.repo, router)
        server = PlayerHttpServer(
            self.repo, SessionService(self.repo, access_secret="s" * 32), ShuffleDeckService(self.repo),
            router, read_mode=self.service,
        )
        self.client = TestClient(TestServer(server.application()))
        await self.client.start_server()

    async def login(self) -> dict[str, str]:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        return {"tgvio_player_session": response.cookies["tgvio_player_session"].value}

    async def test_switching_requires_a_session_and_persists(self) -> None:
        await self.start(self.direct)
        self.assertEqual((await self.client.get("/api/v1/settings/read-mode")).status, 401)
        cookies = await self.login()
        state = await (await self.client.get("/api/v1/settings/read-mode", cookies=cookies)).json()
        self.assertEqual(state, {"mode": "webdav", "direct_available": True})
        response = await self.client.put("/api/v1/settings/read-mode", json={"mode": "direct"}, cookies=cookies)
        self.assertEqual((await response.json())["mode"], "direct")
        self.assertEqual(await self.repo.get_read_mode(), "direct")

    async def test_invalid_cross_site_and_unavailable_choices_are_refused(self) -> None:
        await self.start(None)
        cookies = await self.login()
        bad = await self.client.put("/api/v1/settings/read-mode", json={"mode": "fast"}, cookies=cookies)
        self.assertEqual(bad.status, 400)
        cross = await self.client.put(
            "/api/v1/settings/read-mode", json={"mode": "webdav"}, cookies=cookies,
            headers={"Origin": "https://attacker.invalid"},
        )
        self.assertEqual(cross.status, 403)
        unavailable = await self.client.put("/api/v1/settings/read-mode", json={"mode": "direct"}, cookies=cookies)
        self.assertEqual(unavailable.status, 409)
        self.assertEqual(await self.repo.get_read_mode(), "webdav")


if __name__ == "__main__":
    unittest.main()
