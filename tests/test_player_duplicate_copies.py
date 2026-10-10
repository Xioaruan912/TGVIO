from __future__ import annotations

from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.application.duplicate_copies import DuplicateCopyCleaner
from tgvio_player.domain.catalog import CatalogCover, CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_catalog import WebDavCollectionEntry

A = "a" * 64
B = "b" * 64


class FakeDrive:
    def __init__(self, files: set[str]) -> None:
        self.files = set(files)
        self.deleted: list[str] = []
        self.fail = False

    async def list_collection(self, remote_path: str):
        return tuple(WebDavCollectionEntry(PurePosixPath(f).name, False)
                     for f in self.files if str(PurePosixPath(f).parent) == remote_path)

    async def delete_location(self, package_path: str, relpath: str) -> bool:
        if self.fail:
            return False
        path = f"{package_path}/{relpath}"
        self.deleted.append(path)
        self.files.discard(path)
        return True


def package(package_id: str, path: str, *media: str, cover: str | None = None) -> CatalogPackage:
    return CatalogPackage(
        package_id, path, package_id[0] * 64, None, None,
        tuple(CatalogMedia(m, "video", 100, "video/mp4", 1080, 1920, 5.0) for m in media),
        tuple(CatalogLocation(m, package_id, f"media/{m[:4]}.mp4") for m in media),
        covers=() if cover is None else (CatalogCover(cover, package_id, f"covers/{cover[:4]}.jpg",
                                                      10, "image/jpeg", "sha256"),),
    )


class DuplicateCopyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        # A is stored three times; only the newest copy has a cover. B is stored once.
        await self.repo.apply_package(package("p1", "/Pron/2026-09-01/1", A, B))
        await self.repo.apply_package(package("p2", "/Pron/2026-09-05/1", A))
        await self.repo.apply_package(package("p3", "/Pron/2026-09-09/1", A, cover=A))
        await self.repo.refresh_media_activity()
        self.drive = FakeDrive({
            "/Pron/2026-09-01/1/media/aaaa.mp4", "/Pron/2026-09-01/1/media/bbbb.mp4",
            "/Pron/2026-09-05/1/media/aaaa.mp4", "/Pron/2026-09-09/1/media/aaaa.mp4",
        })
        self.sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.cleaner = DuplicateCopyCleaner(self.repo, self.drive, self.drive, sleep=sleep)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_the_copy_with_the_cover_is_kept_and_the_spares_go(self) -> None:
        result = await self.cleaner.run_once()
        self.assertEqual((result.removed, result.freed_bytes), (2, 200))
        self.assertEqual(sorted(self.drive.deleted),
                         ["/Pron/2026-09-01/1/media/aaaa.mp4", "/Pron/2026-09-05/1/media/aaaa.mp4"])
        self.assertEqual(await self.repo.deletion_location_records(A),
                         [("p3", "/Pron/2026-09-09/1", "media/aaaa.mp4")])
        self.assertIsNotNone(await self.repo.active_media_details(A))
        self.assertEqual(await self.repo.redundant_copies(limit=10), [])
        # A catalog refresh does not bring a removed spare back.
        await self.repo.apply_package(package("p1", "/Pron/2026-09-01/1", A, B))
        await self.repo.refresh_media_activity()
        self.assertEqual(len(await self.repo.deletion_location_records(A)), 1)

    async def test_without_a_cover_the_oldest_copy_is_kept(self) -> None:
        await self.repo.apply_package(package("p3", "/Pron/2026-09-09/1", A))
        await self.repo.refresh_media_activity()
        await self.cleaner.run_once()
        self.assertEqual(await self.repo.deletion_location_records(A),
                         [("p1", "/Pron/2026-09-01/1", "media/aaaa.mp4")])

    async def test_nothing_is_deleted_when_the_kept_copy_is_missing(self) -> None:
        self.drive.files.discard("/Pron/2026-09-09/1/media/aaaa.mp4")
        result = await self.cleaner.run_once()
        self.assertEqual((result.removed, self.drive.deleted), (0, []))
        self.assertEqual(len(await self.repo.deletion_location_records(A)), 3)

    async def test_a_video_being_deleted_is_left_to_the_deletion(self) -> None:
        await self.repo.request_media_deletion(A, now=1, undo_until=2)
        self.assertEqual(await self.repo.redundant_copies(limit=10), [])

    async def test_a_failed_delete_keeps_the_copy_active_and_is_not_retried_in_a_loop(self) -> None:
        self.drive.fail = True
        result = await self.cleaner.run_once()
        self.assertEqual((result.removed, result.failed), (0, 2))
        self.assertEqual(len(await self.repo.deletion_location_records(A)), 3)
        self.assertEqual((await self.cleaner.run_once()).failed, 0)

    async def test_it_waits_while_something_plays(self) -> None:
        playing = [True, True, False]
        self.cleaner._should_pause = lambda: playing.pop(0) if playing else False
        await self.cleaner.run_once(limit=1)
        self.assertEqual(self.sleeps[:2], [20.0, 20.0])
        self.assertEqual(len(self.drive.deleted), 1)


if __name__ == "__main__":
    unittest.main()
