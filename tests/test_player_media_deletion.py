from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.application.media_deletion import (
    RETRY_MAX_SECONDS,
    MediaDeletionService,
    retry_delay,
)
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite


MEDIA = "a" * 64


class Deleter:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.raise_error = False

    async def delete_location(self, package_path: str, relpath: str) -> bool:
        self.calls.append(relpath)
        if self.raise_error:
            raise ConnectionResetError("drive unreachable")
        return not self.fail


class Favorites:
    def __init__(self) -> None:
        self.unfavorited: list[str] = []

    async def unfavorite(self, media_id: str) -> None:
        self.unfavorited.append(media_id)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class MediaDeletionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        await self.repo.apply_package(CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, None, None,
            (CatalogMedia(MEDIA, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(MEDIA, "package", "video.mp4"),
             CatalogLocation(MEDIA, "package", "copy.mp4")),
        ))
        await self.repo.refresh_media_activity()
        self.deleter = Deleter()
        self.favorites = Favorites()
        self.clock = Clock()
        self.sleeps: list[float] = []
        self.removed: list[str] = []

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        async def on_removed(media_id: str) -> None:
            self.removed.append(media_id)

        self.service = MediaDeletionService(
            self.repo, self.deleter, on_removed=on_removed, favorites=self.favorites,
            clock=self.clock, sleep=sleep,
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_retry_waits_grow_to_an_hour_and_stop_growing(self) -> None:
        self.assertEqual([retry_delay(n) for n in range(1, 8)], [60, 120, 240, 480, 960, 1920, 3600])
        self.assertEqual(retry_delay(500), RETRY_MAX_SECONDS)

    async def test_nothing_runs_inside_the_undo_window(self) -> None:
        self.assertEqual(await self.service.request(MEDIA), 6)
        self.assertFalse(await self.service.run_due())
        self.clock.now += 6
        self.assertTrue(await self.service.run_due())

    async def test_a_deletion_that_keeps_failing_is_never_dropped(self) -> None:
        self.deleter.fail = True
        await self.service.request(MEDIA)
        for _ in range(40):
            self.clock.now += RETRY_MAX_SECONDS
            self.assertTrue(await self.service.run_due())
        self.assertEqual(await self.service.status(), {"pending": 1, "retrying": 1})
        # One row however long it fails: the queue does not grow with attempts.
        rows = self.repo._require().execute("SELECT COUNT(*) FROM player_media_deletions").fetchone()[0]
        self.assertEqual(rows, 1)
        due = await self.repo.next_media_deletion_due()
        self.assertLessEqual(due - self.clock.now, RETRY_MAX_SECONDS)

        self.deleter.fail = False
        self.clock.now += RETRY_MAX_SECONDS
        self.assertTrue(await self.service.run_due())
        self.assertEqual(await self.service.status(), {"pending": 0, "retrying": 0})
        self.assertEqual(self.removed, [MEDIA])

    async def test_an_unreachable_drive_is_retried_like_a_refusal(self) -> None:
        self.deleter.raise_error = True
        await self.service.request(MEDIA)
        self.clock.now += 6
        self.assertTrue(await self.service.run_due())
        self.assertEqual(await self.service.status(), {"pending": 1, "retrying": 1})
        self.assertIsNone(await self.repo.active_media_details(MEDIA))

    async def test_files_are_removed_one_at_a_time_with_a_pause_between_calls(self) -> None:
        await self.service.request(MEDIA)
        self.clock.now += 6
        await self.service.run_due()
        self.assertEqual(sorted(self.deleter.calls), ["copy.mp4", "video.mp4"])
        self.assertEqual(self.sleeps, [0.5])

    async def test_a_deleted_favorite_loses_its_favorite_first(self) -> None:
        await self.service.request(MEDIA)
        self.clock.now += 6
        await self.service.run_due()
        self.assertEqual(self.favorites.unfavorited, [MEDIA])

    async def test_undo_restores_visibility_only_before_the_worker_starts(self) -> None:
        await self.service.request(MEDIA)
        self.assertIsNone(await self.repo.active_media_details(MEDIA))
        self.assertTrue(await self.service.cancel(MEDIA))
        self.assertIsNotNone(await self.repo.active_media_details(MEDIA))

        self.deleter.fail = True
        await self.service.request(MEDIA)
        self.clock.now += 6
        await self.service.run_due()
        self.assertFalse(await self.service.cancel(MEDIA))
        self.assertIsNone(await self.repo.active_media_details(MEDIA))

    async def test_undo_closes_with_the_window_even_before_the_worker_ran(self) -> None:
        await self.service.request(MEDIA)
        self.clock.now += 6
        self.assertFalse(await self.service.cancel(MEDIA))
        self.assertIsNone(await self.repo.active_media_details(MEDIA))


if __name__ == "__main__":
    unittest.main()
