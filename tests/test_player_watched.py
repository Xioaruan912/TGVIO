from __future__ import annotations

from pathlib import Path
import random
from tempfile import TemporaryDirectory
import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.feed import unseen_first
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite

DAY = 86400


class DummyReader:
    async def open_range(self, remote_path, byte_range):
        raise AssertionError("streaming is not expected in this test")


class WatchedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.ids = [str(n) * 64 for n in range(1, 5)]
        await self.repo.apply_package(CatalogPackage(
            "package", "TGVIO/2026-10-03/1", "b" * 64, None, None,
            tuple(CatalogMedia(m, "video", 1000 + n, "video/mp4", 1080, 1920, 10.0 + n, codec="h264")
                  for n, m in enumerate(self.ids)),
            tuple(CatalogLocation(m, "package", f"{n}.mp4") for n, m in enumerate(self.ids)),
        ))
        await self.repo.refresh_media_activity()
        self.deck = ShuffleDeckService(self.repo, rng=random.Random(7), recent_exclusion=0,
                                       max_duration_seconds=300)
        server = PlayerHttpServer(self.repo, SessionService(self.repo, access_secret="s" * 32),
                                  self.deck, DummyReader(), large_video_seconds=300)
        self.client = TestClient(TestServer(server.application()))
        await self.client.start_server()
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.cookies = {"tgvio_player_session": response.cookies["tgvio_player_session"].value}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def unwatched(self) -> list[str]:
        response = await self.client.get("/api/v1/videos?category=all&unwatched=true&sort=longest",
                                          cookies=self.cookies)
        self.assertEqual(response.status, 200, await response.text())
        return [item["id"] for item in (await response.json())["items"]]

    async def test_a_watched_video_leaves_the_unwatched_filter(self) -> None:
        response = await self.client.put(f"/api/v1/media/{self.ids[0]}/watched", cookies=self.cookies)
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.unwatched(), self.ids[:0:-1])
        missing = await self.client.put(f"/api/v1/media/{'f' * 64}/watched", cookies=self.cookies)
        self.assertEqual(missing.status, 404)

    async def test_a_mark_older_than_the_forget_window_counts_as_unseen_again(self) -> None:
        now = int(time.time())
        await self.repo.mark_watched(self.ids[0], at=now - 45 * DAY)
        await self.repo.mark_watched(self.ids[1], at=now - 10 * DAY)
        await self.repo.save_long_video_progress(self.ids[2], 5.0)
        self.assertEqual(await self.unwatched(), [self.ids[3], self.ids[0]], "half a month by default")
        await self.repo.set_forget_after_days(None)
        self.assertEqual(await self.unwatched(), [self.ids[3]], "never forgetting keeps every mark")
        response = await self.client.put("/api/v1/settings/watched", json={"forget_after_days": 30},
                                         cookies=self.cookies)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["forget_after_days"], 30)
        self.assertEqual(await self.unwatched(), [self.ids[3], self.ids[0]])
        self.assertEqual(await self.repo.watched_media_ids(), {self.ids[1]})
        await self.repo.set_forget_after_days(7)
        self.assertEqual(await self.unwatched(), [self.ids[3], self.ids[1], self.ids[0]])
        await self.repo.set_forget_after_days(None)
        self.assertEqual(await self.unwatched(), [self.ids[3]])

    async def test_the_forget_window_only_takes_offered_choices(self) -> None:
        body = await (await self.client.get("/api/v1/settings/watched", cookies=self.cookies)).json()
        self.assertEqual(body["forget_after_days"], 15)
        for bad in (0, 31, "30", True):
            response = await self.client.put("/api/v1/settings/watched",
                                             json={"forget_after_days": bad}, cookies=self.cookies)
            self.assertEqual(response.status, 400, bad)
        body = await (await self.client.get("/api/v1/settings/watched", cookies=self.cookies)).json()
        self.assertEqual(body["forget_after_days"], 15, "a rejected value changes nothing")
        self.assertIn(60, body["choices"])

    async def test_the_feed_plays_unseen_videos_first(self) -> None:
        await self.repo.mark_watched(self.ids[0])
        await self.repo.mark_watched(self.ids[1])
        response = await self.client.get("/api/v1/feed?limit=4", cookies=self.cookies)
        self.assertEqual(response.status, 200, await response.text())
        items = [item["id"] for item in (await response.json())["items"]]
        self.assertEqual(set(items[:2]), set(self.ids[2:]))
        self.assertEqual(set(items[2:]), set(self.ids[:2]))


class UnseenFirstTests(unittest.TestCase):
    def test_a_fully_watched_library_is_a_plain_shuffle(self) -> None:
        ids = [str(n) for n in range(10)]
        self.assertEqual(unseen_first(ids, watched=set(ids), rng=random.Random(1)),
                         unseen_first(ids, watched=set(), rng=random.Random(1)))


if __name__ == "__main__":
    unittest.main()
