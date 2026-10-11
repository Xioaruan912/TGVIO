from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aiohttp.test_utils import TestClient, TestServer

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.tags import InvalidTag, clean_tag_name
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite


class DummyReader:
    async def open_range(self, remote_path, byte_range):
        raise AssertionError("streaming is not expected in this test")


class TagNameTests(unittest.TestCase):
    def test_names_are_one_trimmed_line(self) -> None:
        self.assertEqual(clean_tag_name("  黑丝   短发 "), "黑丝 短发")
        for bad in ("", "   ", "x" * 21, "a\u0000b", 3, None):
            with self.assertRaises(InvalidTag):
                clean_tag_name(bad)


class TagHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.ids = [str(n) * 64 for n in range(1, 4)]
        await self.repo.apply_package(CatalogPackage(
            "package", "TGVIO/2026-10-03/1", "b" * 64, None, None,
            tuple(CatalogMedia(m, "video", 1000 + n, "video/mp4", 1080, 1920, 10.0 + n, codec="h264")
                  for n, m in enumerate(self.ids)),
            tuple(CatalogLocation(m, "package", f"{n}.mp4") for n, m in enumerate(self.ids)),
        ))
        await self.repo.refresh_media_activity()
        server = PlayerHttpServer(self.repo, SessionService(self.repo, access_secret="s" * 32),
                                  ShuffleDeckService(self.repo), DummyReader(), large_video_seconds=300)
        self.client = TestClient(TestServer(server.application()))
        await self.client.start_server()
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.cookies = {"tgvio_player_session": response.cookies["tgvio_player_session"].value}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def call(self, method: str, path: str, **kwargs):
        response = await self.client.request(method, path, cookies=self.cookies, **kwargs)
        body = await response.json() if response.content_type == "application/json" else await response.text()
        return response.status, body

    async def test_tag_a_video_and_narrow_the_wall_to_it(self) -> None:
        status, tag = await self.call("POST", "/api/v1/tags", json={"name": "黑丝"})
        self.assertEqual(status, 201)
        again = await self.call("POST", "/api/v1/tags", json={"name": " 黑丝 "})
        self.assertEqual(again[1]["id"], tag["id"], "the same name gives the same tag")
        status, body = await self.call("PUT", f"/api/v1/media/{self.ids[1]}/tags/{tag['id']}")
        self.assertEqual((status, body["ids"]), (200, [tag["id"]]))
        status, body = await self.call("GET", f"/api/v1/videos?category=all&tag={tag['id']}")
        self.assertEqual([item["id"] for item in body["items"]], [self.ids[1]])
        tags = (await self.call("GET", "/api/v1/tags"))[1]["items"]
        self.assertEqual(tags, [{"id": tag["id"], "name": "黑丝", "count": 1}])
        await self.call("DELETE", f"/api/v1/media/{self.ids[1]}/tags/{tag['id']}")
        self.assertEqual((await self.call("GET", f"/api/v1/media/{self.ids[1]}/tags"))[1]["ids"], [])

    async def test_rename_delete_and_bad_input(self) -> None:
        a = (await self.call("POST", "/api/v1/tags", json={"name": "a"}))[1]
        b = (await self.call("POST", "/api/v1/tags", json={"name": "b"}))[1]
        self.assertEqual((await self.call("PATCH", f"/api/v1/tags/{b['id']}", json={"name": "A"}))[0], 400)
        self.assertEqual((await self.call("PATCH", f"/api/v1/tags/{b['id']}", json={"name": "c"}))[0], 200)
        await self.call("PUT", f"/api/v1/media/{self.ids[0]}/tags/{a['id']}")
        self.assertEqual((await self.call("DELETE", f"/api/v1/tags/{a['id']}"))[0], 200)
        self.assertEqual((await self.call("GET", f"/api/v1/media/{self.ids[0]}/tags"))[1]["ids"], [])
        self.assertEqual((await self.call("PUT", f"/api/v1/media/{self.ids[0]}/tags/999"))[0], 404)
        self.assertEqual((await self.call("PUT", f"/api/v1/media/{'f' * 64}/tags/{b['id']}"))[0], 404)
        self.assertEqual((await self.call("POST", "/api/v1/tags", json={"name": ""}))[0], 400)
        self.assertEqual((await self.call("GET", "/api/v1/videos?category=all&tag=0"))[0], 400)


if __name__ == "__main__":
    unittest.main()
