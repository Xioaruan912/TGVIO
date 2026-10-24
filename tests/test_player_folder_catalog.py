from __future__ import annotations

import unittest

from tgvio_player.domain.catalog import CatalogPackage
from tgvio_player.infrastructure.folder_catalog import LibraryCatalogSource, plain_media_id
from tgvio_player.infrastructure.webdav_catalog import WebDavCollectionEntry as Entry


class Dav:
    def __init__(self, entries: dict[str, tuple[Entry, ...]]) -> None:
        self.entries = entries
        self.listed: list[str] = []

    async def list_collection(self, path: str):
        self.listed.append(path)
        return self.entries[path]

    async def get_json(self, path: str, *, max_bytes: int):
        return None


class LibraryCatalogSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_folders_become_packages_and_junk_is_skipped(self) -> None:
        dav = Dav({
            "长视频": (
                Entry("Movie A.mp4", False, '"a"', 900), Entry("ad.html", False, None, 148),
                Entry("Series", True), Entry("_thumbnails", True), Entry(".tgvio", True), Entry("收藏", True),
            ),
            "长视频/Series": (Entry("ep1.mkv", False, None, 500), Entry("readme.txt", False, None, 3)),
            "长视频/.tgvio": (),
        })
        found = await LibraryCatalogSource(dav, roots=("长视频",)).discover()

        self.assertTrue(found.complete_scan)
        by_path = {package.remote_path: package for package in found.packages}
        self.assertEqual(set(by_path), {"长视频", "长视频/Series"})
        root = by_path["长视频"]
        self.assertIsInstance(root, CatalogPackage)
        self.assertEqual([(m.media_id, m.mime_type) for m in root.media],
                         [(plain_media_id(900, "Movie A.mp4"), "video/mp4")])
        self.assertEqual(root.locations[0].remote_relpath, "Movie A.mp4")
        self.assertEqual(by_path["长视频/Series"].media[0].mime_type, "video/x-matroska")
        self.assertNotIn("长视频/_thumbnails", dav.listed)
        self.assertNotIn("长视频/收藏", dav.listed, "the backup is never scanned")

    async def test_a_moved_file_keeps_its_id(self) -> None:
        self.assertEqual(plain_media_id(900, "Movie A.mp4"), plain_media_id(900, "Movie A.mp4"))
        self.assertNotEqual(plain_media_id(900, "Movie A.mp4"), plain_media_id(901, "Movie A.mp4"))

    async def test_a_folder_with_date_folders_is_read_as_archive_packages(self) -> None:
        dav = Dav({
            "短视频": (Entry("2026-09-22", True), Entry("archive", True), Entry("clip.mp4", False, None, 5)),
            "短视频/2026-09-22": (),
        })
        found = await LibraryCatalogSource(dav, roots=("短视频",)).discover()
        self.assertEqual(found.packages, ())
        self.assertTrue(found.complete_scan)
        self.assertNotIn("短视频/archive", dav.listed, "other folders beside packages are left alone")

    async def test_unset_unreadable_or_unsafe_views_never_count_as_complete(self) -> None:
        self.assertFalse((await LibraryCatalogSource(Dav({}), roots=()).discover()).complete_scan)
        self.assertFalse((await LibraryCatalogSource(Dav({}), roots=("missing",)).discover()).complete_scan)
        unsafe = Dav({"v": (Entry("../escape", True),)})
        self.assertFalse((await LibraryCatalogSource(unsafe, roots=("v",)).discover()).complete_scan)


class WebDavRootListingTests(unittest.TestCase):
    def test_the_root_is_the_endpoint_itself_and_children_are_named_from_it(self) -> None:
        from tgvio_player.infrastructure.webdav_aiohttp import AioHttpReadOnlyWebDavClient, WebDavClientSettings

        client = AioHttpReadOnlyWebDavClient(WebDavClientSettings("https://dav.example.test/dav", "u", "p"))
        self.assertEqual(client._url(""), "https://dav.example.test/dav/")
        self.assertEqual(client._root_entry_name("/dav/115/"), "115")
        self.assertEqual(client._root_entry_name("/dav/%E7%9F%AD/"), "短")
        self.assertIsNone(client._root_entry_name("/dav/"), "the root itself is not a child")
        self.assertIsNone(client._root_entry_name("/dav/a/b/"))
        self.assertIsNone(client._root_entry_name("/other/x/"))


if __name__ == "__main__":
    unittest.main()
