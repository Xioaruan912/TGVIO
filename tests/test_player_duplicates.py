from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aiohttp.test_utils import TestClient, TestServer

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.domain.catalog import CatalogCover, CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.duplicates import Candidate, duplicate_groups
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite


class DummyReader:
    async def open_range(self, remote_path, byte_range):
        raise AssertionError("streaming is not expected in this test")


class DuplicateGroupTests(unittest.TestCase):
    def test_same_cover_and_length_group_and_dismissals_split(self) -> None:
        candidates = [
            Candidate("a", "0" * 16, 100.0),
            Candidate("b", "0" * 15 + "7", 101.0),   # 3 bits off, 1 s longer
            Candidate("c", "0" * 16, 140.0),         # same cover, other length
            Candidate("d", "f" * 16, 100.5),         # other cover
            Candidate("e", "0" * 16, 1000.0),
            Candidate("f", "0" * 16, 1025.0),        # within 3% of a long video
        ]
        self.assertEqual(duplicate_groups(candidates, set()), [["a", "b"], ["e", "f"]])
        self.assertEqual(duplicate_groups(candidates, {("a", "b")}), [["e", "f"]])


class DuplicateReviewHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.ids = ["1" * 64, "2" * 64, "3" * 64]
        sizes = [100, 300, 200]
        await self.repo.apply_package(CatalogPackage(
            "package", "TGVIO/2026-10-03/1", "b" * 64, None, None,
            tuple(CatalogMedia(m, "video", s, "video/mp4", 1080, 1920, 60.0, codec="h264")
                  for m, s in zip(self.ids, sizes)),
            tuple(CatalogLocation(m, "package", f"{n}.mp4") for n, m in enumerate(self.ids)),
            covers=tuple(CatalogCover(m, "package", f"{n}.jpg", 10, "image/jpeg", "sha256", phash="0" * 16)
                         for n, m in enumerate(self.ids)),
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

    async def groups(self) -> list[list[str]]:
        response = await self.client.get("/api/v1/duplicates", cookies=self.cookies)
        self.assertEqual(response.status, 200, await response.text())
        return [[item["id"] for item in group["items"]] for group in (await response.json())["groups"]]

    async def test_a_group_lists_the_largest_copy_first_and_can_be_dismissed(self) -> None:
        self.assertEqual(await self.groups(), [[self.ids[1], self.ids[2], self.ids[0]]])
        response = await self.client.post("/api/v1/duplicates/dismiss", cookies=self.cookies,
                                          json={"media_ids": self.ids})
        self.assertEqual((response.status, (await response.json())["dismissed"]), (200, 3))
        self.assertEqual(await self.groups(), [])

    async def test_a_dismissal_needs_two_valid_ids(self) -> None:
        for bad in ([self.ids[0]], ["x", "y"], "nope"):
            response = await self.client.post("/api/v1/duplicates/dismiss", cookies=self.cookies,
                                              json={"media_ids": bad})
            self.assertEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main()
