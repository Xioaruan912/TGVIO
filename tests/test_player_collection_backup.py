from __future__ import annotations

from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.application.collection_backup import CollectionBackupService, folder_names
from tgvio_player.application.ports import DeleteReceipt, RemoteFileStat, WebDavWriteError
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite

A = "a" * 64
B = "b" * 64


class FakeStorage:
    """Files by path; copy reads the archive file sizes given at construction."""

    def __init__(self, archive: dict[str, int]) -> None:
        self.archive = archive
        self.files: dict[str, int] = {}
        self.dirs: set[str] = set()
        self.calls: list[str] = []
        self.fail_copy = False

    async def ensure_directory(self, path: str) -> None:
        self.dirs.add(path)

    async def copy(self, source: str, target: str) -> None:
        self.calls.append("copy")
        if self.fail_copy:
            raise WebDavWriteError("copy", "network_error")
        assert PurePosixPath(source).name == PurePosixPath(target).name
        self.files[target] = self.archive[source]

    async def move(self, source: str, target: str, *, overwrite: bool) -> None:
        self.calls.append("move")
        if source not in self.files:
            raise WebDavWriteError("move", "not_found_or_conflict", 404)
        self.files[target] = self.files.pop(source)

    async def stat(self, path: str) -> RemoteFileStat | None:
        size = self.files.get(path)
        return None if size is None else RemoteFileStat(size, None)

    async def delete(self, path: str) -> DeleteReceipt:
        self.calls.append("delete")
        self.files.pop(path, None)
        return DeleteReceipt(204, True)


class CollectionBackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        await self.repo.apply_package(CatalogPackage(
            "package", "Pron/2026-09-22/1", "c" * 64, None, None,
            (CatalogMedia(A, "video", 100, "video/mp4", 1080, 1920, 2.0),
             CatalogMedia(B, "video", 200, "video/mp4", 1080, 1920, 2.0)),
            (CatalogLocation(A, "package", "media/aaaa.mp4"),
             CatalogLocation(B, "package", "media/bbbb.mp4")),
        ))
        await self.repo.refresh_media_activity()
        self.storage = FakeStorage({
            "Pron/2026-09-22/1/media/aaaa.mp4": 100,
            "Pron/2026-09-22/1/media/bbbb.mp4": 200,
        })

        async def location(media_id: str):
            return await self.repo.active_media_location(media_id)

        self.clock = [1_000_000.0]
        self.backup = CollectionBackupService(
            self.repo, lambda: self.storage, location, clock=lambda: self.clock[0],
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def drain(self) -> None:
        for _ in range(10):
            if not (await self.backup.sync_once()).processed:
                return

    async def test_a_member_is_copied_into_its_collection_folder(self) -> None:
        video = await self.repo.create("视频", "manual", None)
        await self.repo.add_item(video.collection_id, A)
        await self.drain()
        self.assertEqual(self.storage.files, {"Player/视频/aaaa.mp4": 100})
        self.assertEqual(self.storage.calls, ["copy"])
        await self.drain()  # nothing left to do: no second copy
        self.assertEqual(self.storage.calls, ["copy"])

    async def test_rename_moves_and_removal_deletes(self) -> None:
        video = await self.repo.create("视频", "manual", None)
        await self.repo.add_item(video.collection_id, A)
        await self.repo.add_item(video.collection_id, B)
        await self.drain()
        await self.repo.rename(video.collection_id, "好看")
        await self.drain()
        self.assertEqual(set(self.storage.files), {"Player/好看/aaaa.mp4", "Player/好看/bbbb.mp4"})
        self.assertEqual(self.storage.calls.count("copy"), 2)
        await self.repo.remove_item(video.collection_id, A)
        await self.drain()
        self.assertEqual(set(self.storage.files), {"Player/好看/bbbb.mp4"})
        await self.repo.delete(video.collection_id)
        await self.drain()
        self.assertEqual(self.storage.files, {})
        self.assertEqual(await self.repo.collection_backup_status(), {"copies": 0, "failing": 0})

    async def test_smart_collections_are_not_copied(self) -> None:
        smart = await self.repo.create("最近", "smart", "{}")
        await self.repo.add_item(smart.collection_id, A)
        await self.drain()
        self.assertEqual(self.storage.files, {})

    async def test_a_failed_copy_waits_and_is_retried(self) -> None:
        video = await self.repo.create("视频", "manual", None)
        await self.repo.add_item(video.collection_id, A)
        self.storage.fail_copy = True
        result = await self.backup.sync_once()
        self.assertEqual(result.failed, 1)
        self.assertEqual((await self.backup.sync_once()).processed, 0)  # still waiting
        self.storage.fail_copy = False
        self.clock[0] += 3600
        await self.drain()
        self.assertEqual(self.storage.files, {"Player/视频/aaaa.mp4": 100})

    def test_folder_names_are_single_folders_and_unique(self) -> None:
        folders = folder_names(
            [("1", "视频", "t1"), ("2", "视频", "t2"), ("3", "a/b", "t3"), ("4", "Favorites", "t4"), ("5", "..", "t5")],
            {"Favorites"},
        )
        self.assertEqual(folders, {"1": "视频", "2": "视频 (2)", "3": "a／b", "4": "Favorites (2)", "5": "集合"})


if __name__ == "__main__":
    unittest.main()
