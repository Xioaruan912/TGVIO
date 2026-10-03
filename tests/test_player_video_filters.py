from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aiohttp.test_utils import TestClient, TestServer

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.domain.catalog import (
    CatalogCover,
    CatalogLocation,
    CatalogMedia,
    CatalogPackage,
)
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite


class DummyReader:
    async def open_range(self, remote_path, byte_range):
        raise AssertionError("streaming is not expected in this test")


class VideoFilterTests(unittest.IsolatedAsyncioTestCase):
    """Filters and orders on /api/v1/videos, exercised through the real server."""

    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        # Distinct sizes and durations on purpose: an order that accidentally reads the
        # wrong column then produces a different list instead of the same one.
        self.a, self.b, self.c = "1" * 64, "2" * 64, "3" * 64
        package = CatalogPackage(
            "package",
            "TGVIO/2026-10-03/1",
            "b" * 64,
            '"manifest"',
            '"complete"',
            (
                CatalogMedia(self.a, "video", 1000, "video/mp4", 1080, 1920, 12.0, codec="h264"),
                CatalogMedia(self.b, "video", 5000, "video/mp4", 1080, 1920, 600.0, codec="h264"),
                CatalogMedia(self.c, "video", 2000, "video/mp4", 1080, 1920, 1800.0, codec="h264"),
            ),
            (
                CatalogLocation(self.a, "package", "a.mp4", '"etag"'),
                CatalogLocation(self.b, "package", "b.mp4", '"etag"'),
                CatalogLocation(self.c, "package", "c.mp4", '"etag"'),
            ),
            covers=(CatalogCover(self.b, "package", "b.jpg", 4000, "image/jpeg", "sha256"),),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        await self.repo.save_long_video_progress(self.b, 120.0)
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

    async def _ids(self, cookie: str, query: str) -> list[str]:
        response = await self.client.get(
            f"/api/v1/videos?{query}", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        return [item["id"] for item in body["items"]]

    async def test_size_range_filter(self) -> None:
        cookie = await self._login()
        self.assertEqual(await self._ids(cookie, "category=all&min_bytes=1500&max_bytes=3000"), [self.c])

    async def test_date_range_is_absolute_seconds(self) -> None:
        # The stored timestamp is text, so a missing cast makes the comparison always
        # true and every one of these three cases would return all three rows.
        cookie = await self._login()
        now = int(time.time())
        self.assertEqual(await self._ids(cookie, f"category=all&date_from={now + 3600}"), [])
        self.assertEqual(await self._ids(cookie, f"category=all&date_to={now - 3600}"), [])
        self.assertEqual(
            await self._ids(cookie, f"category=all&date_from={now - 3600}&sort=longest"),
            [self.c, self.b, self.a],
        )

    async def test_cover_presence_filter(self) -> None:
        cookie = await self._login()
        self.assertEqual(await self._ids(cookie, "category=all&has_cover=true"), [self.b])
        self.assertEqual(
            await self._ids(cookie, "category=all&has_cover=false&sort=longest"),
            [self.c, self.a],
        )

    async def test_resumable_and_unwatched_filters(self) -> None:
        cookie = await self._login()
        self.assertEqual(await self._ids(cookie, "category=all&resumable=true"), [self.b])
        self.assertEqual(
            await self._ids(cookie, "category=all&unwatched=true&sort=longest"),
            [self.c, self.a],
        )

    async def test_sort_orders(self) -> None:
        cookie = await self._login()
        self.assertEqual(
            await self._ids(cookie, "category=all&sort=longest"), [self.c, self.b, self.a]
        )
        self.assertEqual(
            await self._ids(cookie, "category=all&sort=largest"), [self.b, self.c, self.a]
        )
        # Resume order puts the row with a saved position first and leaves the rest in
        # a stable order behind it.
        self.assertEqual(
            await self._ids(cookie, "category=all&sort=resume"), [self.b, self.a, self.c]
        )

    async def test_random_order_is_stable_per_seed_and_pages_without_repeats(self) -> None:
        cookie = await self._login()
        first = await self._ids(cookie, "category=all&sort=random&seed=7&limit=1&offset=0")
        second = await self._ids(cookie, "category=all&sort=random&seed=7&limit=1&offset=1")
        third = await self._ids(cookie, "category=all&sort=random&seed=7&limit=1&offset=2")
        self.assertEqual(len({first[0], second[0], third[0]}), 3)
        self.assertEqual(
            {first[0], second[0], third[0]}, {self.a, self.b, self.c}
        )
        self.assertEqual(
            await self._ids(cookie, "category=all&sort=random&seed=7&limit=1&offset=0"), first
        )

    async def test_category_still_applies_alongside_a_sort(self) -> None:
        cookie = await self._login()
        self.assertEqual(await self._ids(cookie, "category=long&sort=longest"), [self.c, self.b])
        self.assertEqual(await self._ids(cookie, "category=short&sort=longest"), [self.a])

    async def test_invalid_filters_are_rejected(self) -> None:
        cookie = await self._login()
        for query in (
            "category=all&colour=red",
            "category=all&sort=random",
            "category=all&min_seconds=abc",
            "category=all&min_seconds=-5",
            "category=all&sort=cheapest",
            "category=all&date_from=9999999999&date_to=1",
        ):
            response = await self.client.get(
                f"/api/v1/videos?{query}", cookies={"tgvio_player_session": cookie}
            )
            self.assertEqual(response.status, 400, query)

    async def test_the_legacy_default_order_is_unchanged(self) -> None:
        # No sort parameter: this is what existing clients and the deck already rely on.
        cookie = await self._login()
        self.assertEqual(await self._ids(cookie, "category=all"), [self.a, self.b, self.c])
        self.assertEqual(await self._ids(cookie, "category=long"), [self.c, self.b])

    async def test_the_total_describes_the_same_filtered_set_as_the_page(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            "/api/v1/videos?category=all&has_cover=true",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        self.assertEqual([item["id"] for item in body["items"]], [self.b])
        self.assertEqual(
            body["total"], 1,
            "a filtered page must not report the whole library as its total",
        )


if __name__ == "__main__":
    unittest.main()
