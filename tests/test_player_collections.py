from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.domain.catalog import (
    CatalogLocation,
    CatalogMedia,
    CatalogPackage,
)
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite


class PlayerCollectionRepositoryTests(unittest.IsolatedAsyncioTestCase):
    """Collections are membership rows, and their lifetime follows the media rows."""

    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.media_id, self.other_id = "4" * 64, "5" * 64
        self.package = CatalogPackage(
            "package",
            "TGVIO/2026-10-03/1",
            "b" * 64,
            '"manifest"',
            '"complete"',
            (
                CatalogMedia(self.media_id, "video", 1000, "video/mp4", 1080, 1920, 12.0),
                CatalogMedia(self.other_id, "video", 2000, "video/mp4", 1080, 1920, 900.0),
            ),
            (
                CatalogLocation(self.media_id, "package", "a.mp4", '"etag"'),
                CatalogLocation(self.other_id, "package", "b.mp4", '"etag"'),
            ),
        )
        await self.repo.apply_package(self.package)
        await self.repo.refresh_media_activity()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    def delete_media_row(self, media_id: str) -> None:
        """No application path deletes a media row, so the cascade guard can only be
        proven by deleting one the way an operator's repair would."""
        connection = self.repo._require()
        connection.execute("DELETE FROM media WHERE media_id=?", (media_id,))
        connection.commit()

    async def test_deleting_a_media_row_cascades_out_of_collections(self) -> None:
        collection = await self.repo.create("旅行", "manual", None)
        self.assertTrue(await self.repo.add_item(collection.collection_id, self.media_id))
        self.delete_media_row(self.media_id)
        self.assertEqual(await self.repo.items(collection.collection_id, 50, 0), ())

    async def test_blank_and_overlong_names_are_rejected(self) -> None:
        for bad in ("", "   ", "x" * 61, "bad\nname"):
            with self.assertRaises(ValueError):
                await self.repo.create(bad, "manual", None)

    async def test_add_item_is_idempotent(self) -> None:
        collection = await self.repo.create("想重看", "manual", None)
        self.assertTrue(await self.repo.add_item(collection.collection_id, self.media_id))
        self.assertFalse(await self.repo.add_item(collection.collection_id, self.media_id))

    async def test_member_list_and_count_follow_the_media_lifetime(self) -> None:
        # A retired video leaves the list and the count, but its membership row
        # survives: when the archive supplies the video again, so does the collection.
        collection = await self.repo.create("旅行", "manual", None)
        await self.repo.add_item(collection.collection_id, self.media_id)
        await self.repo.add_item(collection.collection_id, self.other_id)
        await self.repo.record_deleted_location(self.media_id, "package", "a.mp4")
        self.assertTrue(await self.repo.finalize_media_deletion(self.media_id))
        self.assertEqual(
            await self.repo.items(collection.collection_id, 50, 0), (self.other_id,)
        )
        self.assertEqual(await self.repo.counts(), {collection.collection_id: 1})
        self.assertEqual(
            self.repo._require().execute(
                "SELECT COUNT(*) FROM collection_items WHERE collection_id=?",
                (collection.collection_id,),
            ).fetchone()[0],
            2,
            "the membership row is kept while the media is inactive",
        )
        relisted = CatalogPackage(
            "package2",
            "TGVIO/2026-10-04/1",
            "c" * 64,
            '"manifest2"',
            '"complete2"',
            (CatalogMedia(self.media_id, "video", 1000, "video/mp4", 1080, 1920, 12.0),),
            (CatalogLocation(self.media_id, "package2", "moved.mp4", '"etag2"'),),
        )
        await self.repo.apply_package(relisted)
        await self.repo.refresh_media_activity()
        self.assertEqual(
            await self.repo.items(collection.collection_id, 50, 0),
            (self.media_id, self.other_id),
        )

    async def test_members_keep_the_order_they_were_added_in(self) -> None:
        collection = await self.repo.create("旅行", "manual", None)
        await self.repo.add_item(collection.collection_id, self.other_id)
        await self.repo.add_item(collection.collection_id, self.media_id)
        self.assertEqual(
            await self.repo.items(collection.collection_id, 50, 0),
            (self.other_id, self.media_id),
        )
        self.assertEqual(await self.repo.items(collection.collection_id, 1, 1), (self.media_id,))

    async def test_remove_item_is_idempotent(self) -> None:
        collection = await self.repo.create("旅行", "manual", None)
        await self.repo.add_item(collection.collection_id, self.media_id)
        self.assertTrue(await self.repo.remove_item(collection.collection_id, self.media_id))
        self.assertFalse(await self.repo.remove_item(collection.collection_id, self.media_id))

    async def test_add_item_to_an_unknown_collection_or_media_changes_nothing(self) -> None:
        collection = await self.repo.create("旅行", "manual", None)
        self.assertFalse(await self.repo.add_item("no-such-collection", self.media_id))
        self.assertFalse(await self.repo.add_item(collection.collection_id, "9" * 64))
        self.assertEqual(await self.repo.items(collection.collection_id, 50, 0), ())

    async def test_rename_set_rules_and_delete_report_whether_a_row_changed(self) -> None:
        collection = await self.repo.create("旅行", "manual", None)
        self.assertTrue(await self.repo.rename(collection.collection_id, "重看"))
        self.assertTrue(await self.repo.set_rules(collection.collection_id, '{"min_seconds": 10}'))
        self.assertFalse(await self.repo.rename("no-such-collection", "x"))
        self.assertFalse(await self.repo.set_rules("no-such-collection", None))
        self.assertFalse(await self.repo.delete("no-such-collection"))
        updated = await self.repo.get(collection.collection_id)
        self.assertEqual((updated.name, updated.rules_json), ("重看", '{"min_seconds": 10}'))
        self.assertTrue(await self.repo.delete(collection.collection_id))
        self.assertIsNone(await self.repo.get(collection.collection_id))

    async def test_kind_and_rules_are_validated(self) -> None:
        for kind in ("builtin", "", "MANUAL"):
            with self.assertRaises(ValueError):
                await self.repo.create("旅行", kind, None)
        for bad in ("{", "[]", "not json", '"text"', "x" * 5000):
            with self.assertRaises(ValueError):
                await self.repo.create("旅行", "smart", bad)
        stored = await self.repo.create("旅行", "smart", '{"min_seconds": 10}')
        self.assertEqual((stored.kind, stored.rules_json), ("smart", '{"min_seconds": 10}'))

    async def test_list_keeps_creation_order_and_counts_empty_collections(self) -> None:
        first = await self.repo.create("旅行", "manual", None)
        second = await self.repo.create("重看", "manual", None)
        await self.repo.add_item(first.collection_id, self.media_id)
        self.assertEqual(
            [item.collection_id for item in await self.repo.list()],
            [first.collection_id, second.collection_id],
        )
        self.assertEqual(
            [item.name for item in await self.repo.list()], ["旅行", "重看"]
        )
        self.assertEqual(
            await self.repo.counts(), {first.collection_id: 1, second.collection_id: 0}
        )

    async def test_sort_order_is_writable_and_orders_the_list(self) -> None:
        first = await self.repo.create("旅行", "manual", None)
        second = await self.repo.create("重看", "manual", None)
        self.assertTrue(await self.repo.set_sort_order(second.collection_id, -1))
        self.assertFalse(await self.repo.set_sort_order("no-such-collection", 0))
        self.assertEqual(
            [item.collection_id for item in await self.repo.list()],
            [second.collection_id, first.collection_id],
            "a lower sort_order wins, and it survives the rowid tiebreaker",
        )

    async def test_favorite_counts_do_not_need_a_page(self) -> None:
        # The builtin collection's badge must not fetch a page of joined rows. The
        # per-session half is covered end to end by the HTTP builtin-count test.
        self.assertEqual(await self.repo.count_global_favorites(), 0)
        await self.repo.set_global_favorite(self.media_id, True)
        self.assertEqual(await self.repo.count_global_favorites(), 1)
        await self.repo.record_deleted_location(self.media_id, "package", "a.mp4")
        await self.repo.finalize_media_deletion(self.media_id)
        self.assertEqual(
            await self.repo.count_global_favorites(), 0,
            "a retired video is not a favourite the page can show",
        )


if __name__ == "__main__":
    unittest.main()
