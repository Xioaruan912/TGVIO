from __future__ import annotations

import asyncio
import unittest

from tgvio_player.application.warm_backfill import MediaWarmBackfill


class FakeCache:
    def __init__(self, cached: set[str] | None = None) -> None:
        self.cached = cached or set()
        self.warmed: list[str] = []

    def has_chunk(self, key: str, index: int) -> bool:
        return key in self.cached

    async def warm(
        self, key: str, package: str, relpath: str, size: int, length: int, *, whole_below: int = 0
    ) -> None:
        self.warmed.append(key)
        self.cached.add(key)


class FakeRepository:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    async def count_active_videos(self) -> int:
        return len(self.ids)

    async def list_video_ids(self, **kwargs: object) -> list[str]:
        return list(self.ids)

    async def active_media_details(self, media_id: str) -> dict[str, object]:
        return {"size_bytes": 10 * 1024 * 1024}

    async def active_media_location(self, media_id: str):
        return ("package", f"{media_id}.mp4", None)


class MediaWarmBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_warms_all_and_skips_cached(self) -> None:
        ids = ["a" * 64, "b" * 64, "c" * 64]
        cache = FakeCache(cached={"b" * 64})
        backfill = MediaWarmBackfill(
            cache, FakeRepository(ids), head_bytes=16 * 1024 * 1024, workers=2
        )
        stop = asyncio.Event()
        await backfill.run(stop)
        self.assertEqual(set(cache.warmed), {"a" * 64, "c" * 64})

    async def test_pauses_then_resumes(self) -> None:
        cache = FakeCache()
        flag = {"paused": True}

        def should_pause() -> bool:
            if flag["paused"]:
                flag["paused"] = False
                return True
            return False

        backfill = MediaWarmBackfill(
            cache,
            FakeRepository(["d" * 64]),
            head_bytes=1024,
            workers=1,
            should_pause=should_pause,
            pause_seconds=0.01,
        )
        await backfill.run(asyncio.Event())
        self.assertEqual(cache.warmed, ["d" * 64])


if __name__ == "__main__":
    unittest.main()
