from __future__ import annotations

import asyncio
from pathlib import Path
import random
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.application.playback import StartupCacheKey, StartupRangeCache
from tgvio_player.application.streaming import prepare_stream_request
from tgvio_player.domain.auth import token_digest, verify_access_secret
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.ranges import ByteRange, RangeNotSatisfiable, parse_single_range
from tgvio_player.domain.storage_settings import PlayerStorageSettings
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_read import (
    ReadOnlyWebDavAdapter,
    WebDavRangeResponse,
)


class RangeTests(unittest.TestCase):
    def test_single_range_forms_and_stream_headers(self) -> None:
        self.assertIsNone(parse_single_range(None, size_bytes=10))
        self.assertEqual(parse_single_range("bytes=0-", size_bytes=10), ByteRange(0, 9))
        self.assertEqual(parse_single_range("bytes=3-", size_bytes=10), ByteRange(3, 9))
        self.assertEqual(parse_single_range("bytes=3-7", size_bytes=10), ByteRange(3, 7))
        self.assertEqual(prepare_stream_request("bytes=3-", size_bytes=10).content_range, "bytes 3-9/10")

    def test_suffix_multi_invalid_and_unsatisfiable_ranges_are_416(self) -> None:
        for value in ("bytes=-4", "bytes=0-1,3-4", "items=0-1", "bytes=9-8", "bytes=10-"):
            with self.subTest(value=value), self.assertRaises(RangeNotSatisfiable):
                parse_single_range(value, size_bytes=10)
        self.assertEqual(RangeNotSatisfiable.status_code, 416)


class FakeReadClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ByteRange | None]] = []

    async def open_range(self, remote_path: str, byte_range: ByteRange | None) -> WebDavRangeResponse:
        self.calls.append((remote_path, byte_range))

        async def body():
            yield b"part"

        return WebDavRangeResponse(206, "video/mp4", 4, "bytes 0-3/4", '"etag"', body())


class ReadAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_only_accepts_catalog_owned_safe_paths(self) -> None:
        client = FakeReadClient()
        adapter = ReadOnlyWebDavAdapter(client)
        response = await adapter.open_range("TGVIO/2026-09-22/1", "video.mp4", ByteRange(0, 3))
        self.assertEqual(response.status, 206)
        self.assertEqual(client.calls, [("TGVIO/2026-09-22/1/video.mp4", ByteRange(0, 3))])
        with self.assertRaises(ValueError):
            await adapter.open_range("TGVIO/2026-09-22/1", "../video.mp4", None)


class StartupCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_lru_is_byte_bounded_and_etag_aware(self) -> None:
        cache = StartupRangeCache(max_entries=2, max_bytes=4)
        key_a = StartupCacheKey("a", '"one"', ByteRange(0, 1))
        key_b = StartupCacheKey("b", '"one"', ByteRange(0, 1))
        key_c = StartupCacheKey("c", '"one"', ByteRange(0, 1))

        async def fetch(value: bytes) -> bytes:
            return value

        for key, value in ((key_a, b"aa"), (key_b, b"bb"), (key_c, b"cc")):
            _, task = await cache.acquire(key, lambda value=value: fetch(value))
            self.assertIsNotNone(task)
            await task
            await asyncio.sleep(0)
        cached, task = await cache.acquire(key_a, lambda: fetch(b"wrong"))
        self.assertIsNone(cached)
        assert task is not None
        await task
        await asyncio.sleep(0)
        same_media_new_etag = StartupCacheKey("c", '"two"', ByteRange(0, 1))
        cached, task = await cache.acquire(same_media_new_etag, lambda: fetch(b"xx"))
        self.assertIsNone(cached)
        assert task is not None
        await task
        await asyncio.sleep(0)
        self.assertLessEqual(cache.byte_size, 4)

    async def test_inflight_fetch_is_deduplicated_and_only_last_release_cancels(self) -> None:
        cache = StartupRangeCache(max_entries=1, max_bytes=10)
        key = StartupCacheKey("a", '"one"', ByteRange(0, 1))
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fetch() -> bytes:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return b"ok"

        _, first = await cache.acquire(key, fetch)
        await started.wait()
        _, second = await cache.acquire(key, fetch)
        self.assertIs(first, second)
        await cache.release(key)
        self.assertFalse(first.cancelled())
        release.set()
        self.assertEqual(await second, b"ok")
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)

    async def test_last_inflight_lease_cancels_upstream(self) -> None:
        cache = StartupRangeCache(max_entries=1, max_bytes=10)
        key = StartupCacheKey("a", '"one"', ByteRange(0, 1))
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fetch() -> bytes:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        _, task = await cache.acquire(key, fetch)
        assert task is not None
        await started.wait()
        await cache.release(key)
        await cancelled.wait()
        with self.assertRaises(asyncio.CancelledError):
            await task


class PlayerStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.media_ids = [f"{index:064x}" for index in range(1, 5)]
        package = CatalogPackage(
            package_id="package",
            remote_path="TGVIO/2026-09-22/1",
            manifest_sha256="a" * 64,
            manifest_etag='"manifest"',
            complete_etag='"complete"',
            media=tuple(CatalogMedia(media_id, "video", 10) for media_id in self.media_ids),
            locations=tuple(CatalogLocation(media_id, "package", f"{index}.mp4", '"file"') for index, media_id in enumerate(self.media_ids)),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_login_stores_only_digest_and_cookie_is_secure(self) -> None:
        service = SessionService(self.repo, access_secret="x" * 32, clock=lambda: 100)
        self.assertIsNone(await service.login("wrong"))
        cookie = await service.login("x" * 32)
        assert cookie is not None
        self.assertTrue(await service.authenticate(cookie.value))
        self.assertIn("HttpOnly; Secure; SameSite=Strict", cookie.set_cookie_value())
        self.assertTrue(verify_access_secret("x" * 32, "x" * 32))
        self.assertFalse(verify_access_secret("x" * 32, "y" * 32))
        self.assertFalse(await self.repo.has_player_session(cookie.value, now=100))
        await service.logout(cookie.value)
        self.assertFalse(await service.authenticate(cookie.value))

    async def test_shuffle_cycle_is_persistent_unique_and_recent_excluded(self) -> None:
        digest = token_digest("session")
        await self.repo.create_player_session(digest, expires_at=9_999_999_999)
        deck = ShuffleDeckService(self.repo, rng=random.Random(7), recent_exclusion=2)
        first = await deck.next_items(digest, limit=4)
        self.assertEqual(set(first), set(self.media_ids))
        self.assertEqual(len(first), len(set(first)))
        second = await deck.next_items(digest, limit=1)
        self.assertEqual(len(second), 1)
        self.assertNotIn(second[0], first[-2:])

    async def test_favorites_are_idempotent_and_do_not_change_deck(self) -> None:
        digest = token_digest("favorite-session")
        await self.repo.create_player_session(digest, expires_at=9_999_999_999)
        deck = ShuffleDeckService(self.repo, rng=random.Random(1))
        await deck.favorite(digest, self.media_ids[0])
        await deck.favorite(digest, self.media_ids[0])
        self.assertTrue(await self.repo.is_favorite(digest, self.media_ids[0]))
        self.assertEqual(set(await deck.next_items(digest, limit=4)), set(self.media_ids))
        await deck.unfavorite(digest, self.media_ids[0])
        await deck.unfavorite(digest, self.media_ids[0])
        self.assertFalse(await self.repo.is_favorite(digest, self.media_ids[0]))

    async def test_favorite_keyset_pages_cover_more_than_two_hundred_items(self) -> None:
        digest = token_digest("large-favorite-session")
        extra_ids = [f"{index:064x}" for index in range(100, 301)]
        package = CatalogPackage(
            "many-favorites", "TGVIO/2026-09-22/2", "e" * 64,
            '"many-manifest"', '"many-complete"',
            tuple(CatalogMedia(media_id, "video", 30) for media_id in extra_ids),
            tuple(CatalogLocation(media_id, "many-favorites", f"{media_id}.mp4") for media_id in extra_ids),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        await self.repo.create_player_session(digest, expires_at=9_999_999_999)
        all_ids = [*self.media_ids, *extra_ids]
        for media_id in all_ids:
            await self.repo.set_favorite(digest, media_id, enabled=True)
        self.repo._require().execute(
            "UPDATE favorites SET created_at=123 WHERE token_digest=?", (digest,)
        )
        deck = ShuffleDeckService(self.repo)
        received: list[str] = []
        cursor: tuple[int, str] | None = None
        while True:
            page = await deck.favorite_page(digest, limit=6, cursor=cursor)
            received.extend(media_id for media_id, _created_at in page[:5])
            if len(page) <= 5:
                break
            cursor = page[4][1], page[4][0]
        self.assertEqual(len(received), len(all_ids))
        self.assertEqual(len(set(received)), len(all_ids))
        self.assertEqual(received, sorted(all_ids))

    async def test_global_favorite_settings_outbox_and_copy_locations(self) -> None:
        media_id = self.media_ids[0]
        await self.repo.set_global_favorite(media_id, True)
        await self.repo.set_global_favorite(media_id, True)
        first_page = await self.repo.list_global_favorite_page(limit=10, before=None)
        self.assertEqual([row[0] for row in first_page], [media_id])

        initial = await self.repo.get_storage_settings()
        self.assertEqual(initial.endpoint_url, "https://file.722225.xyz")
        self.assertEqual(initial.player_root, "115/Pron/99_TGPLAYER")
        self.assertEqual(initial.favorites_dir, "99_收藏")
        configured = PlayerStorageSettings(
            endpoint_url="https://dav.example.test",
            player_root="player/root",
            favorites_dir="Saved",
            username_ciphertext=b"encrypted-user",
            password_ciphertext=b"encrypted-password",
            revision=3,
        )
        await self.repo.save_storage_settings(configured)
        self.assertEqual(await self.repo.get_storage_settings(), configured)

        await self.repo.enqueue_favorite_sync(media_id, "upload")
        await self.repo.enqueue_favorite_sync(media_id, "upload")
        jobs = await self.repo.claim_favorite_sync(limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0].media_id, jobs[0].operation, jobs[0].attempts), (media_id, "upload", 1))
        await self.repo.finish_favorite_sync(jobs[0].job_id, "retry", "http_503")
        retry = await self.repo.claim_favorite_sync(limit=10)
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0].attempts, 2)
        await self.repo.finish_favorite_sync(retry[0].job_id, "synced", None)
        self.assertEqual(await self.repo.claim_favorite_sync(limit=10), [])

        await self.repo.save_favorite_location(media_id, "99_收藏/item.mp4", 10, "video/mp4")
        self.assertEqual(
            await self.repo.list_favorite_locations(),
            [(media_id, "99_收藏/item.mp4", 10, "video/mp4")],
        )
        await self.repo.set_global_favorite(media_id, False)
        await self.repo.set_global_favorite(media_id, False)
        self.assertEqual(await self.repo.list_global_favorite_page(limit=10, before=None), [])

    async def test_legacy_session_favorites_are_merged_when_player_migrates(self) -> None:
        path = Path(self.tmp.name) / "legacy.sqlite3"
        migrations = Path(self.tmp.name) / "legacy-migrations"
        migrations.mkdir()
        source_migrations = Path(__file__).parents[1] / "src/tgvio_player/infrastructure/migrations"
        for migration in sorted(source_migrations.glob("000[1-4]_*.sql")):
            (migrations / migration.name).write_bytes(migration.read_bytes())

        legacy = PlayerCatalogRepositorySQLite(path, migrations_dir=migrations)
        await legacy.open()
        await legacy.apply_package(CatalogPackage(
            "legacy", "TGVIO/2026-09-22/legacy", "a" * 64, None, None,
            tuple(CatalogMedia(media_id, "video", 10) for media_id in self.media_ids[:2]),
            tuple(CatalogLocation(media_id, "legacy", f"{index}.mp4") for index, media_id in enumerate(self.media_ids[:2])),
        ))
        await legacy.create_player_session("1" * 64, expires_at=9_999_999_999)
        await legacy.create_player_session("2" * 64, expires_at=9_999_999_999)
        await legacy.set_favorite("1" * 64, self.media_ids[0], enabled=True)
        await legacy.set_favorite("2" * 64, self.media_ids[0], enabled=True)
        await legacy.set_favorite("2" * 64, self.media_ids[1], enabled=True)
        await legacy.close()

        migrated = PlayerCatalogRepositorySQLite(path)
        await migrated.open()
        rows = await migrated.list_global_favorite_page(limit=10, before=None)
        self.assertEqual({row[0] for row in rows}, set(self.media_ids[:2]))
        self.assertEqual(len(rows), 2)
        await migrated.close()

        reopened = PlayerCatalogRepositorySQLite(path)
        await reopened.open()
        try:
            repeated = await reopened.list_global_favorite_page(limit=10, before=None)
            self.assertEqual(repeated, rows)
        finally:
            await reopened.close()

    async def test_archive_groups_follow_active_catalog_locations(self) -> None:
        sibling_id = "5" * 64
        sibling = CatalogPackage(
            package_id="sibling-package",
            remote_path="TGVIO/2026-09-22/2",
            manifest_sha256="c" * 64,
            manifest_etag='"manifest-2"',
            complete_etag='"complete-2"',
            media=(CatalogMedia(sibling_id, "video", 20),),
            locations=(CatalogLocation(sibling_id, "sibling-package", "sibling.mp4"),),
        )
        await self.repo.apply_package(sibling)
        await self.repo.refresh_media_activity()

        media_groups = await self.repo.list_media_groups(sibling_id)
        self.assertEqual(len(media_groups), 1)
        group_id, label = media_groups[0]
        self.assertEqual(label, "2026-09-22")
        self.assertEqual(
            await self.repo.list_group_video_ids(group_id, after_id=None, limit=20),
            sorted([*self.media_ids, sibling_id]),
        )

        await self.repo.deactivate_packages_not_seen({"package"})
        await self.repo.refresh_media_activity()
        self.assertEqual(await self.repo.list_media_groups(sibling_id), [])
        self.assertEqual(
            await self.repo.list_group_video_ids(group_id, after_id=None, limit=20),
            sorted(self.media_ids),
        )


if __name__ == "__main__":
    unittest.main()
