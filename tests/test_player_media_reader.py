from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.domain.storage_settings import PlayerStorageSettings
from tgvio_player.infrastructure.player_media_reader import PlayerMediaReader
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse


class FakeArchiveReader:
    def __init__(self, status: int = 404) -> None:
        self.status = status
        self.calls: list[tuple[str, str, ByteRange | None]] = []

    async def open_range(self, package: str, relpath: str, byte_range: ByteRange | None):
        self.calls.append((package, relpath, byte_range))

        async def body():
            yield b"archive"

        return WebDavRangeResponse(self.status, "video/mp4", 7, None, None, body())


class FakeFavoriteReader:
    def __init__(self, body: bytes = b"copy") -> None:
        self.body = body
        self.calls: list[tuple[str, ByteRange | None]] = []
        self.closed = False

    async def open(self) -> None:
        return None

    async def open_range(self, path: str, byte_range: ByteRange | None):
        self.calls.append((path, byte_range))

        async def body():
            yield self.body

        return WebDavRangeResponse(206, "video/mp4", len(self.body), None, None, body())

    async def close(self) -> None:
        self.closed = True


class PlayerMediaReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.media_id = "a" * 64
        await self.repo.apply_package(CatalogPackage(
            "pkg", "archive/pkg", "b" * 64, None, None,
            (CatalogMedia(self.media_id, "video", 7, "video/mp4"),),
            (CatalogLocation(self.media_id, "pkg", "source.mp4"),),
        ))
        await self.repo.refresh_media_activity()
        await self.repo.save_storage_settings(PlayerStorageSettings(
            "https://file.example", "Player", "Favorites",
        ))
        self.archive = FakeArchiveReader()
        self.favorite = FakeFavoriteReader()
        self.reader = PlayerMediaReader(
            self.repo,
            self.archive,
            lambda: self.repo.get_storage_settings(),
            lambda _settings: ("user", "password"),
            lambda _endpoint, _username, _password: self.favorite,
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_archive_404_falls_back_to_verified_favorite_copy(self) -> None:
        await self.repo.save_favorite_location(
            self.media_id, "Favorites/copy.mp4", 4, "video/mp4",
        )
        response = await self.reader.open_range(
            "archive/pkg", "source.mp4", ByteRange(0, 3),
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(self.favorite.calls, [("Player/Favorites/copy.mp4", ByteRange(0, 3))])
        self.assertEqual([chunk async for chunk in response.body], [b"copy"])
        self.assertTrue(self.favorite.closed)

    async def test_catalog_missing_archive_uses_player_favorite_location(self) -> None:
        await self.repo.record_deleted_location(self.media_id, "pkg", "source.mp4")
        await self.repo.finalize_media_deletion(self.media_id)
        await self.repo.save_favorite_location(
            self.media_id, "Favorites/copy.mp4", 4, "video/mp4",
        )
        location = await self.repo.active_media_location(self.media_id)
        self.assertEqual(location, ("__player_favorite__", "Favorites/copy.mp4", None))
        response = await self.reader.open_range(*location[:2], ByteRange(0, 3))
        self.assertEqual(response.status, 206)
        self.assertEqual(self.archive.calls, [])


if __name__ == "__main__":
    unittest.main()
