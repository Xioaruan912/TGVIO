from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp.test_utils import TestClient, TestServer
from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.application.auth import SessionService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.domain.catalog import CatalogCover, CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite


def mid(n):
    return f'{n:064x}'


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / 'offline.sqlite3')
        await self.repo.open()
        await self.package('large', 'Private/2026-09-22/1', range(1, 906))
        await self.package('same-day', 'Private/2026-09-22/2', [1, 906])
        await self.package('next-day', 'Private/2026-09-23/1', [1, 907])
        await self.package('legacy', 'Private/2026/09/22/private-batch', [1, 908])
        await self.package('unknown', 'Private/private-name', [909])
        await self.package('bad-date', 'Private/2026-02-30/1', [910])
        await self.package('rendition-only', 'Private/2026-09-24/1', [911], variant=True)
        await self.package('inactive', 'Private/2026-09-25/1', [912])
        self.repo._require().execute("UPDATE catalog_packages SET active=0 WHERE package_id='inactive'")
        self.repo._require().commit()
        await self.repo.refresh_media_activity()
        self.reader = Mock()
        self.reader.open_range = AsyncMock(side_effect=AssertionError('no archive reads'))
        self.server = PlayerHttpServer(
            self.repo, SessionService(self.repo, access_secret='s' * 32),
            ShuffleDeckService(self.repo), self.reader, large_video_seconds=300,
        )
        self.server._schedule_prefetch = AsyncMock(side_effect=AssertionError('no warming'))
        self.server._faststart = Mock()
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()
        login = await self.client.post('/api/v1/auth/login', json={'secret': 's' * 32})
        self.cookies = {'tgvio_player_session': login.cookies['tgvio_player_session'].value}

    async def package(self, name, path, ids, variant=False):
        ids = list(ids)
        media = tuple(CatalogMedia(mid(i), 'video', 1000, 'video/mp4', 1080, 1920,
                                   None if i == 1 else (600 if i % 2 == 0 else 12),
                                   variant_of=mid(1) if variant else None) for i in ids)
        await self.repo.apply_package(CatalogPackage(
            name, path, 'b' * 64, None, None, media,
            tuple(CatalogLocation(mid(i), name, f'private-{i}.mp4') for i in ids),
        ))

    async def asyncTearDown(self):
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def get(self, suffix, status=200):
        response = await self.client.get('/api/v1/library/' + suffix, cookies=self.cookies)
        self.assertEqual(response.status, status, await response.text())
        return await response.json() if status == 200 else None

    async def folder(self):
        folders = (await self.get('folders?date=2026-09-22'))['items']
        return next(f for f in folders if f['video_count'] == 905)

    async def test_dates_unique_totals_and_mixed_basis(self):
        data = await self.get('dates')
        self.assertEqual(data['total_videos'], 910)
        self.assertEqual(data['items'], [
            {'date': '2026-09-23', 'basis': 'directory_v2', 'video_count': 2, 'folder_count': 1},
            {'date': '2026-09-22', 'basis': 'mixed', 'video_count': 907, 'folder_count': 3},
            {'date': None, 'basis': 'unknown', 'video_count': 2, 'folder_count': 2},
        ])

    async def test_folders_are_packages_opaque_stable_and_private(self):
        data = await self.get('folders?date=2026-09-22')
        self.assertEqual(data['total'], 3)
        self.assertEqual(sorted(f['video_count'] for f in data['items']), [2, 2, 905])
        self.assertEqual({f['date_basis'] for f in data['items']}, {'directory_v2', 'directory_legacy_utc'})
        self.assertEqual(data, await self.get('folders?date=2026-09-22'))
        for f in data['items']:
            self.assertEqual(set(f), {'id', 'label', 'date', 'date_basis', 'video_count'})
            self.assertRegex(f['id'], r'^folder_[0-9a-f]{64}$')
        self.assertNotIn('Private', str(data))
        self.assertNotIn('private-batch', str(data))
        self.assertEqual((await self.get('folders?date=unknown'))['total'], 2)

    async def test_membership_is_independent_of_groups(self):
        data = await self.get('folders?media_id=' + mid(1))
        self.assertEqual(data['total'], 4)
        unknown = await self.get('folders?media_id=' + mid(909))
        self.assertEqual(unknown['items'][0]['date'], None)
        self.assertEqual((await self.get('folders?media_id=' + mid(911)))['total'], 0)

    async def test_items_advertise_an_archive_cover_without_reading_the_archive(self):
        digest = mid(920)
        await self.repo.apply_package(CatalogPackage(
            'covers', 'Private/2026-09-26/1', 'b' * 64, None, None,
            (CatalogMedia(digest, 'video', 1000, 'video/mp4', 1080, 1920, 12.0),),
            (CatalogLocation(digest, 'covers', 'private-920.mp4'),),
            (CatalogCover(digest, 'covers', 'cover/private-920-cover.jpg', 4096,
                          'image/jpeg', 'reuse-publish-thumbnail-v1'),),
        ))
        await self.repo.refresh_media_activity()
        folders = (await self.get('folders?date=2026-09-26'))['items']
        page = await self.get('videos?folder_id=' + folders[0]['id'])
        cover = await self.repo.active_cover(digest)
        self.assertEqual(
            {item['id']: item['cover_url'] for item in page['items']},
            {digest: f"/api/v1/media/{digest}/cover?v={cover['version']}"},
        )
        # The DTO only states where a cover would come from; browsing reads nothing.
        self.reader.open_range.assert_not_awaited()

    async def test_full_catalog_filter_before_keyset_and_count_consistency(self):
        folder = await self.folder()
        for category, expected in [('all', list(range(1, 906))),
                                   ('short', [i for i in range(1, 906) if i % 2]),
                                   ('long', [i for i in range(1, 906) if not i % 2])]:
            ids, cursor = [], ''
            while True:
                page = await self.get(f'videos?folder_id={folder["id"]}&category={category}&limit=20' + cursor)
                self.assertEqual(page['total'], len(expected))
                self.assertEqual(page['folder'], folder)
                ids.extend(item['id'] for item in page['items'])
                self.assertLessEqual(len(page['items']), 20)
                if not page['has_more']:
                    self.assertIsNone(page['next_cursor'])
                    break
                self.assertEqual(page['next_cursor'], page['items'][-1]['id'])
                cursor = '&cursor=' + page['next_cursor']
            self.assertEqual(ids, [mid(i) for i in expected])
            self.assertEqual(len(ids), len(set(ids)))
        self.server._schedule_prefetch.assert_not_awaited()
        self.server._faststart.schedule.assert_not_called()
        self.reader.open_range.assert_not_awaited()

    async def test_auth_and_strict_validation(self):
        for suffix in ['dates', 'folders?date=2026-09-22', 'videos?folder_id=bad']:
            response = await self.client.get('/api/v1/library/' + suffix)
            self.assertEqual(response.status, 401)
        folder = await self.folder()
        for query in ['folders', 'folders?date=2026-02-30', 'folders?date=20260922',
                      'folders?date=2026-9-22', 'folders?date=../x', 'folders?date=',
                      'folders?media_id=bad', 'folders?date=unknown&media_id=' + mid(1),
                      'videos?folder_id=bad', 'videos?folder_id=' + folder['id'] + '&cursor=bad',
                      'videos?folder_id=' + folder['id'] + '&cursor=',
                      'videos?folder_id=' + folder['id'] + '&category=medium',
                      'videos?folder_id=' + folder['id'] + '&limit=0',
                      'videos?folder_id=' + folder['id'] + '&limit=21']:
            await self.get(query, 400)
        await self.get('videos?folder_id=folder_' + '0' * 64, 404)
        self.assertEqual((await self.get('folders?date=2026-09-26'))['items'], [])

    async def test_renditions_excluded_from_old_groups_too(self):
        await self.package('rendition-mixed', 'Private/2026-09-22/3', [913], variant=True)
        await self.repo.refresh_media_activity()
        groups = await self.repo.list_media_groups(mid(1))
        ids = await self.repo.list_group_video_ids(groups[0][0], after_id=None, limit=2000)
        self.assertNotIn(mid(913), ids)

    async def test_metadata_is_read_only_no_deck_or_schema_or_warm_effects(self):
        conn = self.repo._require()
        changes = conn.total_changes
        schema = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        schema = [tuple(r) for r in schema]
        writes = []
        conn.set_trace_callback(lambda sql: writes.append(sql) if
                                sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
                                else None)
        try:
            await self.get('dates')
            await self.get('folders?date=unknown')
            await self.get('folders?media_id=' + mid(1))
            folder = await self.folder()
            page = await self.get('videos?folder_id=' + folder['id'])
            self.assertNotIn('cache=1', str(page))
            self.assertNotIn('private-', str(page))
            self.assertNotIn('remote_path', str(page))
            self.assertEqual(len(page['items']), 20)
            self.assertEqual(writes, [])
            self.assertEqual(conn.total_changes, changes)
            self.assertEqual([tuple(r) for r in conn.execute("SELECT sql FROM sqlite_master ORDER BY name")], schema)
            self.server._schedule_prefetch.assert_not_awaited()
            self.server._faststart.schedule.assert_not_called()
            self.reader.open_range.assert_not_awaited()
        finally:
            conn.set_trace_callback(None)

    async def test_duplicate_locations_and_inactive_predicate(self):
        conn = self.repo._require()
        conn.execute("INSERT INTO media_locations(package_id, media_id, remote_relpath) VALUES (?, ?, ?)",
                     ('large', mid(1), 'second-private-copy.mp4'))
        conn.execute("UPDATE media_locations SET active=0 WHERE package_id='same-day'")
        conn.execute("UPDATE media SET active=0 WHERE media_id=?", (mid(909),))
        conn.commit()
        folders = await self.get('folders?date=2026-09-22')
        self.assertEqual(folders['total'], 2)
        folder = await self.folder()
        page = await self.get('videos?folder_id=' + folder['id'])
        self.assertEqual(page['total'], 905)
        self.assertEqual(len({i['id'] for i in page['items']}), 20)
        self.assertEqual((await self.get('folders?date=unknown'))['total'], 1)
        dates = await self.get('dates')
        self.assertEqual(dates['total_videos'], 908)
        self.assertEqual(dates['items'][1]['video_count'], 906)

    async def test_category_boundary_and_filtered_cursor(self):
        conn = self.repo._require()
        conn.execute("UPDATE media SET duration_seconds=300 WHERE media_id=?", (mid(2),))
        conn.commit()
        folder = await self.folder()
        page = await self.get('videos?folder_id=' + folder['id'] + '&category=short')
        self.assertEqual(page['items'][1]['id'], mid(2))
        self.assertEqual(page['items'][1]['category'], 'short')
        self.assertEqual(page['total'], 454)
        long_page = await self.get('videos?folder_id=' + folder['id'] + '&category=long&cursor=' + mid(2))
        self.assertEqual([i['id'] for i in long_page['items']], [mid(i) for i in range(4, 44, 2)])
        self.assertEqual(long_page['total'], 451)
        empty = await self.get('videos?folder_id=' + folder['id'] + '&cursor=' + mid(909))
        self.assertEqual(empty['items'], [])
        self.assertEqual(empty['total'], 905)
        self.assertFalse(empty['has_more'])
        self.assertIsNone(empty['next_cursor'])
        await self.get('folders?date=unknown&date=2026-09-22', 400)
        await self.get('folders?media_id=' + mid(1) + '%27%20OR%201=1', 400)

    async def test_folder_identity_survives_repository_reopen(self):
        data = await self.get('folders?date=2026-09-22')
        await self.repo.close()
        await self.repo.open()
        self.assertEqual(data, await self.get('folders?date=2026-09-22'))

    async def test_deleted_or_retired_tail_cursor_continues_keyset(self):
        folder = await self.folder()
        conn = self.repo._require()
        for mode, category, tail in [('delete', 'all', 20), ('retire', 'long', 42)]:
            with self.subTest(mode=mode):
                first = await self.get('videos?folder_id=' + folder['id'] + '&category=' + category)
                self.assertEqual(len(first['items']), 20)
                cursor = first['next_cursor']
                self.assertEqual(cursor, mid(tail))
                if mode == 'delete':
                    conn.execute("DELETE FROM media_locations WHERE media_id=?", (cursor,))
                else:
                    conn.execute("UPDATE media_locations SET active=0 WHERE media_id=?", (cursor,))
                conn.commit()
                await self.repo.refresh_media_activity()
                self.assertIsNone(await self.repo.active_media_details(cursor))
                second = await self.get('videos?folder_id=' + folder['id'] + '&category=' + category + '&cursor=' + cursor)
                step = 1 if category == 'all' else 2
                self.assertEqual([i['id'] for i in second['items']],
                                 [mid(tail + step * i) for i in range(1, 21)])
                expected_total = 904 if category == 'all' else 450
                self.assertEqual(second['total'], expected_total)
                fresh = await self.get('videos?folder_id=' + folder['id'] + '&category=' + category)
                self.assertEqual(second['total'], fresh['total'])
                self.assertEqual(second['folder']['video_count'], 904 if mode == 'delete' else 903)
                self.assertTrue(second['has_more'])

    async def test_foreign_or_nonexistent_cursor_never_expands_folder_scope(self):
        folders = await self.get('folders?media_id=' + mid(907))
        folder = folders['items'][0]
        # Valid keys can belong to another folder or have no catalog row at all.
        for cursor in [mid(20), mid(500), mid(0)]:
            page = await self.get('videos?folder_id=' + folder['id'] + '&cursor=' + cursor)
            expected = [i for i in [1, 907] if mid(i) > cursor]
            self.assertEqual([i['id'] for i in page['items']], [mid(i) for i in expected])
            self.assertEqual(page['total'], 2)
            self.assertFalse(page['has_more'])
        page = await self.get('videos?folder_id=' + folder['id'] + '&cursor=' + mid(9999))
        self.assertEqual(page['items'], [])
        self.assertEqual(page['total'], 2)
