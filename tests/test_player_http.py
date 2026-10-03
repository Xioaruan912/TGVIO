from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.adapters.http.diagnostics import classify_http_outcome, fingerprint
from tgvio_player.adapters.http.server import resolve_client
from tgvio_player.application.auth import SessionService
from tgvio_player.application.favorite_backup import FavoriteBackupService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.application.playback import StartupRangeCache
from tgvio_player.application.player_recovery import PlayerRecoveryService
from tgvio_player.application.ports import WebDavWriteError
from tgvio_player.domain.catalog import CatalogCover, CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.auth import token_digest
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.domain.storage_settings import PlayerStorageSettings
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.player_crypto import PlayerStateCipher
from tgvio_player.infrastructure.webdav_write import AioHttpWebDavWriteClient
from tgvio_player.infrastructure.cover_mirror import CoverMirror, CoverMirrorCounters
from tgvio_player.infrastructure.webdav_read import ReadOnlyWebDavAdapter, WebDavRangeResponse


class ClosableBody:
    def __init__(self, chunks: list[bytes], *, wait: asyncio.Event | None = None) -> None:
        self._chunks = chunks
        self._wait = wait
        self.closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._wait is not None:
            await self._wait.wait()
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.closed.set()


class FakeReadClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ByteRange | None]] = []
        self.body = ClosableBody([b"abcd"])
        self.status = 206

    async def open_range(self, remote_path: str, byte_range: ByteRange | None) -> WebDavRangeResponse:
        self.calls.append((remote_path, byte_range))
        body = self.body
        if byte_range is None:
            return WebDavRangeResponse(self.status, "video/mp4", 4, None, '"etag"', body)
        return WebDavRangeResponse(
            self.status,
            "video/mp4",
            byte_range.length,
            f"bytes {byte_range.start}-{byte_range.end}/4",
            '"etag"',
            body,
        )


class FakeDeleteClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failures: set[tuple[str, str]] = set()

    async def delete_location(self, package_path: str, remote_relpath: str) -> bool:
        self.calls.append((package_path, remote_relpath))
        return (package_path, remote_relpath) not in self.failures


class FakeStorageClient:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.move_status: int | None = None
        self.get_status: int | None = None

    async def ensure_directory(self, path: str) -> None:
        self.calls.append(("MKCOL", path))

    async def put_stream(self, path: str, chunks, *, size_bytes: int, content_type: str):
        self.calls.append(("PUT", path))
        payload = bytearray()
        async for chunk in chunks:
            payload.extend(chunk)
        self.files[path] = bytes(payload)
        return type("Receipt", (), {"status_code": 201, "size_bytes": len(payload), "etag": None})()

    async def stat(self, path: str):
        self.calls.append(("HEAD", path))
        value = self.files.get(path)
        return None if value is None else type("Stat", (), {"size_bytes": len(value), "etag": None})()

    async def delete(self, path: str):
        self.calls.append(("DELETE", path))
        self.files.pop(path, None)
        return type("Receipt", (), {"status_code": 204, "deleted": True})()

    async def get_bytes(self, path: str, *, max_bytes: int):
        self.calls.append(("GET", path))
        if self.get_status is not None:
            raise WebDavWriteError("get", "server_error", self.get_status)
        value = self.files.get(path)
        return value if value is None or len(value) <= max_bytes else None

    async def move(self, source: str, target: str, *, overwrite: bool):
        self.calls.append(("MOVE", source))
        if self.move_status is not None:
            category = "move_unsupported" if self.move_status in {405, 501} else "server_error"
            raise WebDavWriteError("move", category, self.move_status)
        self.files[target] = self.files.pop(source)

    async def open_stream(self, path: str):
        payload = self.files[path]

        async def chunks():
            yield payload

        return len(payload), "video/mp4", chunks()


class PlayerHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.media_id = "a" * 64
        package = CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, '"manifest"', '"complete"',
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        self.read_client = FakeReadClient()
        self.delete_client = FakeDeleteClient()
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.read_client),
            deleter=self.delete_client,
            max_streams=2,
            max_streams_per_client=1,
            startup_cache=StartupRangeCache(max_entries=2, max_bytes=8),
            startup_range_bytes=4,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def _login(self) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.assertEqual(response.status, 200)
        return response.cookies["tgvio_player_session"].value

    async def _enable_storage_services(self) -> dict[str, FakeStorageClient]:
        await self.client.close()
        self.cipher = PlayerStateCipher(base64.urlsafe_b64encode(b"r" * 32).decode().rstrip("="))
        clients: dict[str, FakeStorageClient] = {}

        def factory(endpoint: str, _username: str, _password: str) -> FakeStorageClient:
            return clients.setdefault(endpoint, FakeStorageClient())

        recovery = PlayerRecoveryService(self.repo, self.cipher, factory)
        writer = factory("https://dav.example.test", "", "")

        async def source(media_id: str):
            location = await self.repo.active_media_location(media_id)
            assert location is not None
            result = await self.read_client.open_range(location[0], None)
            return result.content_length or 0, result.content_type, result.body

        backup = FavoriteBackupService(self.repo, writer, source, recovery)
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.read_client),
            deleter=self.delete_client,
            favorite_backup=backup,
            recovery_service=recovery,
            storage_client_factory=factory,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()
        return clients

    async def test_auth_feed_and_favorite_never_expose_location(self) -> None:
        self.assertEqual((await self.client.get("/api/v1/feed")).status, 401)
        self.assertEqual((await self.client.post("/api/v1/auth/login", json={"secret": "wrong"})).status, 401)
        cookie = await self._login()
        self.assertEqual((await self.client.get(
            "/api/v1/feed?token=leak", cookies={"tgvio_player_session": cookie}
        )).status, 400)
        response = await self.client.get("/api/v1/feed?limit=1", cookies={"tgvio_player_session": cookie})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["items"][0]["id"], self.media_id)
        self.assertFalse(body["items"][0]["favorite"])
        self.assertNotIn("remote_path", str(body))
        favorite = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(favorite.status, 200)
        details = await self.client.get(
            f"/api/v1/media/{self.media_id}", cookies={"tgvio_player_session": cookie}
        )
        self.assertTrue((await details.json())["favorite"])
        self.assertEqual((await self.client.delete(
            f"/api/v1/media/{self.media_id}/favorite", cookies={"tgvio_player_session": cookie}
        )).status, 200)

    async def test_storage_settings_are_authenticated_write_only_and_encrypted(self) -> None:
        await self._enable_storage_services()
        self.assertEqual((await self.client.get("/api/v1/settings/storage")).status, 401)
        cookie = await self._login()
        response = await self.client.get(
            "/api/v1/settings/storage", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["endpoint_url"], "https://webdav.example.invalid/dav")
        self.assertFalse(body["storage_configured"])
        self.assertFalse(body["credentials_configured"])
        self.assertNotIn("username", body)
        self.assertNotIn("password", body)
        self.assertNotIn("ciphertext", str(body))
        self.assertNotIn("recovery_key", body)

        saved = await self.client.put(
            "/api/v1/settings/storage",
            json={"username": "alice", "password": "secret-value"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(saved.status, 200)
        self.assertTrue((await saved.json())["credentials_configured"])
        row = await self.repo.get_storage_settings()
        self.assertNotEqual(row.username_ciphertext, b"alice")
        self.assertNotEqual(row.password_ciphertext, b"secret-value")
        self.assertNotIn(b"secret-value", row.password_ciphertext or b"")

        preserved = await self.client.put(
            "/api/v1/settings/storage", json={"username": "", "password": ""},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(preserved.status, 200)
        self.assertEqual(await self.repo.get_storage_settings(), row)

    async def test_existing_storage_endpoint_is_configured_at_revision_zero(self) -> None:
        await self._enable_storage_services()
        await self.repo.save_storage_settings(PlayerStorageSettings(
            endpoint_url="https://dav.example.test/dav", player_root="Player",
            favorites_dir="Favorites", revision=0,
        ))
        cookie = await self._login()
        response = await self.client.get(
            "/api/v1/settings/storage", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["storage_configured"])

    async def test_storage_validation_same_origin_and_probe_cleanup(self) -> None:
        clients = await self._enable_storage_services()
        cookie = await self._login()
        bad = await self.client.put(
            "/api/v1/settings/storage",
            json={"endpoint_url": "http://127.0.0.1", "player_root": "../escape"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(bad.status, 400)
        cross_origin = await self.client.put(
            "/api/v1/settings/storage", json={},
            headers={"Origin": "https://attacker.invalid"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(cross_origin.status, 403)

        response = await self.client.post(
            "/api/v1/settings/storage/test",
            json={
                "endpoint_url": "https://dav.example.test", "player_root": "test-root",
                "username": "user", "password": "pass",
            },
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ok"])
        client = clients["https://dav.example.test"]
        operations = [operation for operation, _path in client.calls]
        self.assertIn("PUT", operations)
        self.assertIn("GET", operations)
        self.assertNotIn("MOVE", operations)
        self.assertIn("HEAD", operations)
        self.assertIn("DELETE", operations)
        probe_paths = [path for operation, path in client.calls if operation == "PUT"]
        self.assertEqual(len(probe_paths), 2)
        self.assertEqual(probe_paths[0], probe_paths[1])
        self.assertNotIn(probe_paths[0], client.files)
        self.assertTrue(probe_paths[0].startswith("test-root/.player-probe-"))
        self.assertTrue(probe_paths[0].endswith(".bin"))

    async def test_storage_test_identifies_operation_returning_server_error(self) -> None:
        clients = await self._enable_storage_services()
        cookie = await self._login()
        client = clients.setdefault("https://dav.example.test", FakeStorageClient())
        client.get_status = 502
        response = await self.client.post(
            "/api/v1/settings/storage/test",
            json={
                "endpoint_url": "https://dav.example.test", "player_root": "test-root",
                "username": "user", "password": "pass",
            },
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["operation"], "get")
        self.assertEqual(result["category"], "server_error")
        self.assertEqual(result["status_code"], 502)

    async def test_global_favorites_are_visible_to_a_second_session(self) -> None:
        await self._enable_storage_services()
        first_cookie = await self._login()
        second_cookie = await self._login()
        favorite = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            cookies={"tgvio_player_session": first_cookie},
        )
        self.assertEqual(favorite.status, 200)
        payload = await favorite.json()
        self.assertEqual(payload["sync_status"], "pending")
        response = await self.client.get(
            "/api/v1/favorites", cookies={"tgvio_player_session": second_cookie}
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual([item["id"] for item in data["items"]], [self.media_id])
        self.assertTrue(data["items"][0]["favorite"])

    async def test_storage_retry_requeues_failed_favorite_sync(self) -> None:
        await self._enable_storage_services()
        cookie = await self._login()
        await self.repo.set_global_favorite(self.media_id, True)
        await self.repo.enqueue_favorite_sync(self.media_id, "upload")
        job = (await self.repo.claim_favorite_sync(limit=1))[0]
        await self.repo.finish_favorite_sync(job.job_id, "failed", "source_not_found")
        response = await self.client.post(
            "/api/v1/settings/storage/retry", cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["retried"], 1)
        self.assertEqual((await self.repo.claim_favorite_sync(limit=1))[0].media_id, self.media_id)

    async def test_feed_limits_background_range_prefetch_to_five_items(self) -> None:
        cookie = await self._login()
        self.server._deck.next_items = AsyncMock(return_value=[self.media_id] * 7)
        self.server._schedule_prefetch = AsyncMock()

        response = await self.client.get(
            "/api/v1/feed?limit=20&cache=1",
            cookies={"tgvio_player_session": cookie},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(len((await response.json())["items"]), 7)
        self.assertEqual(self.server._schedule_prefetch.await_count, 5)

    async def test_head_prefetch_is_limited_to_one_cache_chunk(self) -> None:
        cache = Mock(chunk_bytes=1024)
        self.server._range_cache = cache

        await self.server._schedule_prefetch(self.media_id, {"size_bytes": 4096})

        self.assertEqual(cache.prefetch_head.call_args.args[4], 1024)
        self.assertEqual(cache.prefetch_head.call_args.kwargs["whole_below"], 1024)

    async def test_delete_media_removes_every_registered_file_but_no_folder(self) -> None:
        duplicate = CatalogPackage(
            "duplicate-package", "TGVIO/2026-09-23/2", "c" * 64,
            '"manifest-2"', '"complete-2"',
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "duplicate-package", "nested/copy.mp4"),),
        )
        await self.repo.apply_package(duplicate)
        await self.repo.refresh_media_activity()
        cookie = await self._login()

        response = await self.client.delete(
            f"/api/v1/media/{self.media_id}",
            cookies={"tgvio_player_session": cookie},
        )

        self.assertEqual(response.status, 200)
        self.assertCountEqual(
            self.delete_client.calls,
            [
                ("TGVIO/2026-09-22/1", "video.mp4"),
                ("TGVIO/2026-09-23/2", "nested/copy.mp4"),
            ],
        )
        self.assertTrue(all(relpath.endswith(".mp4") for _, relpath in self.delete_client.calls))
        self.assertEqual((await response.json())["deleted_copies"], 2)
        self.assertIsNone(await self.repo.active_media_details(self.media_id))

    async def test_permanent_delete_removes_still_before_original(self):
        cover_path = "cover/backfill/" + "f" * 64 + ".jpg"
        package = CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, None, None,
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4"),),
            covers=(CatalogCover(self.media_id, "package", cover_path, 4, "image/jpeg", "bounded-frame-v2"),),
        )
        await self.repo.apply_package(package)
        cookie = await self._login()
        response = await self.client.delete(f"/api/v1/media/{self.media_id}",
                                            cookies={"tgvio_player_session": cookie})
        body = await response.json()
        self.assertTrue(body["removed"])
        self.assertEqual(body["deleted_covers"], 1)
        self.assertEqual(body["deleted_copies"], 1)
        self.assertEqual([path for _, path in self.delete_client.calls], [cover_path, "video.mp4"])
        self.assertIsNone(await self.repo.active_cover(self.media_id))

    async def test_failed_cover_delete_keeps_original_and_reports_partial_failure(self):
        cover_path = "cover/backfill/" + "f" * 64 + ".jpg"
        package = CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, None, None,
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4"),),
            covers=(CatalogCover(self.media_id, "package", cover_path, 4, "image/jpeg", "bounded-frame-v2"),),
        )
        await self.repo.apply_package(package)
        self.delete_client.failures.add(("TGVIO/2026-09-22/1", cover_path))
        cookie = await self._login()
        response = await self.client.delete(f"/api/v1/media/{self.media_id}",
                                            cookies={"tgvio_player_session": cookie})
        body = await response.json()
        self.assertFalse(body["removed"])
        self.assertEqual(body["failed_covers"], 1)
        self.assertEqual([path for _, path in self.delete_client.calls], [cover_path])
        self.assertIsNotNone(await self.repo.active_media_details(self.media_id))

    async def _add_delete_rendition(self):
        variant_id = "e" * 64
        package = CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, None, None,
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),
             CatalogMedia(variant_id, "video", 3, "video/mp4", 270, 480, 2.0,
                          "mp4", "h264", self.media_id, "480p", 1200000)),
            (CatalogLocation(self.media_id, "package", "video.mp4"),
             CatalogLocation(variant_id, "package", "renditions/480.mp4")),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        return variant_id, package

    async def test_delete_includes_renditions_and_tombstones_prevent_restore(self):
        variant_id, package = await self._add_delete_rendition()
        cookie = await self._login()
        response = await self.client.delete(
            f"/api/v1/media/{self.media_id}",
            cookies={"tgvio_player_session": cookie},
        )
        body = await response.json()
        self.assertTrue(body["removed"])
        self.assertEqual(body["deleted_copies"], 2)
        self.assertEqual(self.delete_client.calls,
                         [("TGVIO/2026-09-22/1", "renditions/480.mp4"),
                          ("TGVIO/2026-09-22/1", "video.mp4")])
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        self.assertIsNone(await self.repo.active_media_details(self.media_id))
        self.assertIsNone(await self.repo.active_media_details(variant_id))

    async def test_failed_rendition_deletion_keeps_original_and_reports_failure(self):
        _, _package = await self._add_delete_rendition()
        self.delete_client.failures.add(("TGVIO/2026-09-22/1", "renditions/480.mp4"))
        cookie = await self._login()
        response = await self.client.delete(
            f"/api/v1/media/{self.media_id}",
            cookies={"tgvio_player_session": cookie},
        )
        body = await response.json()
        self.assertFalse(body["removed"])
        self.assertEqual(body["failed_copies"], 2)
        self.assertEqual(self.delete_client.calls, [("TGVIO/2026-09-22/1", "renditions/480.mp4")])
        self.assertIsNotNone(await self.repo.active_media_details(self.media_id))

    async def test_deleted_location_tombstone_prevents_catalog_resurrection(self) -> None:
        cookie = await self._login()
        response = await self.client.delete(
            f"/api/v1/media/{self.media_id}",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 200)

        package = CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, '"manifest"', '"complete"',
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()

        self.assertIsNone(await self.repo.active_media_details(self.media_id))

    async def test_delete_media_reports_partial_failure_and_keeps_remaining_copy(self) -> None:
        duplicate = CatalogPackage(
            "duplicate-package", "TGVIO/2026-09-23/2", "c" * 64,
            '"manifest-2"', '"complete-2"',
            (CatalogMedia(self.media_id, "video", 4, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "duplicate-package", "copy.mp4"),),
        )
        await self.repo.apply_package(duplicate)
        await self.repo.refresh_media_activity()
        self.delete_client.failures.add(("TGVIO/2026-09-23/2", "copy.mp4"))
        cookie = await self._login()

        response = await self.client.delete(
            f"/api/v1/media/{self.media_id}",
            cookies={"tgvio_player_session": cookie},
        )

        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["deleted_copies"], 1)
        self.assertEqual(body["failed_copies"], 1)
        self.assertFalse(body["removed"])
        self.assertIsNotNone(await self.repo.active_media_details(self.media_id))

    async def test_delete_playing_media_releases_its_stream_slot(self) -> None:
        class BlockingRangeCache:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.discarded = asyncio.Event()

            async def prime(self, *args, **kwargs) -> None:
                return None

            async def stream(self, *args, **kwargs):
                self.started.set()
                await self.discarded.wait()
                raise RuntimeError("media cache discarded")
                yield b""  # pragma: no cover - keeps this an async generator

            async def discard(self, media_id: str) -> None:
                self.discarded.set()

            def stats(self) -> dict[str, object]:
                return {}

        cache = BlockingRangeCache()
        self.server._range_cache = cache
        cookie = await self._login()
        playing = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=1-3"},
            cookies={"tgvio_player_session": cookie},
        )
        await asyncio.wait_for(cache.started.wait(), timeout=1)
        self.assertEqual(self.server.active_playback_streams, 1)

        deleted = await self.client.delete(
            f"/api/v1/media/{self.media_id}",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(deleted.status, 200)
        self.assertTrue((await deleted.json())["removed"])
        with self.assertRaises(Exception):
            await playing.read()
        for _ in range(20):
            if self.server.active_playback_streams == 0:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.server.active_playback_streams, 0)

    async def test_range_headers_security_and_invalid_range(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=1-3"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b"abc")
        self.assertEqual(self.read_client.calls, [("TGVIO/2026-09-22/1/video.mp4", ByteRange(1, 3))])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertTrue(self.read_client.body.closed.is_set())
        invalid = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=0-1,2-3"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(invalid.status, 416)
        empty_suffix = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=-0"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(empty_suffix.status, 416)

    async def test_suffix_range_serves_the_tail(self) -> None:
        cookie = await self._login()
        for header, expected_range, expected_length in (
            ("bytes=-2", ByteRange(2, 3), "2"),
            ("bytes=-10", ByteRange(0, 3), "4"),
        ):
            self.read_client.body = ClosableBody([b"abcd"])
            self.read_client.calls.clear()
            response = await self.client.get(
                f"/api/v1/media/{self.media_id}/stream",
                headers={"Range": header},
                cookies={"tgvio_player_session": cookie},
            )
            self.assertEqual(response.status, 206, header)
            self.assertEqual(response.headers["Content-Range"],
                             f"bytes {expected_range.start}-{expected_range.end}/4", header)
            self.assertEqual(response.headers["Content-Length"], expected_length, header)
            # The reader must be asked for the tail slice, not the head.
            self.assertEqual(self.read_client.calls[-1][1], expected_range, header)
            await response.read()

    async def test_head_reports_headers_without_touching_the_archive(self) -> None:
        cookie = await self._login()
        ranged = await self.client.head(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=1-3"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(ranged.status, 206)
        self.assertEqual(await ranged.read(), b"")
        self.assertEqual(ranged.headers["Content-Range"], "bytes 1-3/4")
        self.assertEqual(ranged.headers["Content-Length"], "3")
        self.assertEqual(ranged.headers["Accept-Ranges"], "bytes")
        self.assertEqual(ranged.headers["Content-Type"], "video/mp4")
        self.assertIn("filename=", ranged.headers["Content-Disposition"])

        whole = await self.client.head(
            f"/api/v1/media/{self.media_id}/stream",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(whole.status, 200)
        self.assertEqual(whole.headers["Content-Length"], "4")

        # Probing must not read the archive or consume a playback slot.
        self.assertEqual(self.read_client.calls, [])
        self.assertEqual(self.server.active_playback_streams, 0)

    async def test_stream_disposition_names_the_clip_and_download_forces_attachment(self) -> None:
        cookie = await self._login()
        name = f"{fingerprint(self.media_id)}.mp4"

        async def fetch(query: str, byte_range: str):
            # FakeReadClient bodies are single-use, exactly like a real upstream
            # response, so each request needs a fresh one.
            self.read_client.body = ClosableBody([b"abcd"])
            response = await self.client.get(
                f"/api/v1/media/{self.media_id}/stream{query}",
                headers={"Range": byte_range},
                cookies={"tgvio_player_session": cookie},
            )
            self.assertEqual(response.status, 206)
            await response.read()
            return response

        # Playback, the cached startup range and both download flag spellings
        # must agree on the filename, so a browser's "save as" is meaningful
        # whichever path served the bytes.
        self.assertEqual(
            (await fetch("", "bytes=1-3")).headers["Content-Disposition"],
            f'inline; filename="{name}"',
        )
        self.assertEqual(
            (await fetch("", "bytes=0-3")).headers["Content-Disposition"],
            f'inline; filename="{name}"',
        )
        self.assertEqual(
            (await fetch("?download=1", "bytes=1-3")).headers["Content-Disposition"],
            f'attachment; filename="{name}"',
        )
        self.assertEqual(
            (await fetch("?download=true", "bytes=0-3")).headers["Content-Disposition"],
            f'attachment; filename="{name}"',
        )

    async def test_stream_capacity_releases_on_client_cancellation(self) -> None:
        cookie = await self._login()
        wait = asyncio.Event()
        self.read_client.body = ClosableBody([b"later"], wait=wait)
        first_body = self.read_client.body
        first = asyncio.create_task(self.client.get(
            f"/api/v1/media/{self.media_id}/stream", cookies={"tgvio_player_session": cookie}
        ))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.client.get(
            f"/api/v1/media/{self.media_id}/stream", cookies={"tgvio_player_session": cookie}
        ))
        await asyncio.sleep(0.05)
        self.assertFalse(second.done(), "same-client playback should wait for the previous request to release")
        first_response = await first
        self.read_client.body = ClosableBody([b"x"])
        first_response.close()
        await asyncio.wait_for(first_body.closed.wait(), timeout=1)
        second_response = await asyncio.wait_for(second, timeout=1)
        self.assertEqual(second_response.status, 200)
        self.read_client.status = 503
        failed = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(failed.status, 502)
        self.assertTrue(self.read_client.body.closed.is_set())

    async def test_stream_and_playback_diagnostics_share_session_id(self) -> None:
        cookie = await self._login()
        playback_session = "a1" * 16
        with self.assertLogs("tgvio_player.diagnostics", level="INFO") as captured:
            stream = await self.client.get(
                f"/api/v1/media/{self.media_id}/stream?playback_session={playback_session}",
                headers={"Range": "bytes=1-3"},
                cookies={"tgvio_player_session": cookie},
            )
            await stream.read()
            event = await self.client.post(
                "/api/v1/diagnostics/playback-event",
                json={
                    "event": "media_play",
                    "media_id": self.media_id,
                    "category": "short",
                    "playback_session": playback_session,
                },
                cookies={"tgvio_player_session": cookie},
            )
        self.assertEqual(stream.status, 206)
        self.assertEqual(event.status, 204)
        self.assertTrue(any(
            '"event":"http_request"' in line
            and f'"media_id":"{self.media_id}"' in line
            and f'"session":"{playback_session}"' in line
            and '"stream_kind":"video"' in line
            and '"outcome":"stream_ok"' in line
            for line in captured.output
        ))
        self.assertTrue(any(
            '"event":"frontend_playback"' in line
            and f'"media_id":"{self.media_id}"' in line
            and f'"session":"{playback_session}"' in line
            and '"action":"media_play"' in line
            for line in captured.output
        ))

    async def test_diagnostic_outcomes_separate_missing_media_from_disconnects(self) -> None:
        self.assertEqual(classify_http_outcome(404, "HTTPNotFound", is_stream=True), "not_found")
        self.assertEqual(classify_http_outcome(503, "HTTPBadGateway", is_stream=True), "server_error")
        self.assertEqual(classify_http_outcome(206, "ConnectionResetError", is_stream=True), "client_disconnected")
        self.assertEqual(classify_http_outcome(429, "HTTPTooManyRequests", is_stream=True), "capacity_limited")

    async def test_foreground_stream_waits_for_capacity_instead_of_returning_429(self) -> None:
        self.assertTrue(await self.server._acquire_stream("preload-client", preload=True))
        self.assertTrue(await self.server._acquire_stream("playback-client"))
        waiting = asyncio.create_task(self.server._acquire_stream("next-playback-client"))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done(), "foreground playback should wait briefly for a stream slot")

        await self.server._release_stream("preload-client", preload=True)
        self.assertTrue(await asyncio.wait_for(waiting, timeout=1))

        await self.server._release_stream("playback-client")
        await self.server._release_stream("next-playback-client")

    async def test_preload_preserves_one_global_slot_for_foreground_playback(self) -> None:
        self.assertTrue(await self.server._acquire_stream("playback-client"))
        diagnostics: dict[str, object] = {}
        acquired = await self.server._acquire_stream(
            "speculative-client", preload=True, diagnostics=diagnostics
        )
        self.assertFalse(acquired)
        self.assertEqual(diagnostics["reason"], "playback_capacity_reserved")
        await self.server._release_stream("playback-client")

    async def test_startup_range_is_cached_with_catalog_etag_and_range_headers(self) -> None:
        cookie = await self._login()
        first = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=0-3"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(first.status, 206)
        self.assertEqual(await first.read(), b"abcd")
        self.assertEqual(first.headers["Content-Range"], "bytes 0-3/4")
        self.assertEqual(first.headers["Content-Length"], "4")
        self.assertEqual(first.headers["ETag"], '"etag"')
        self.assertTrue(self.read_client.body.closed.is_set())

        self.read_client.body = ClosableBody([b"wrong"])
        second = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=0-3"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(await second.read(), b"abcd")
        self.assertEqual(len(self.read_client.calls), 1)
        self.assertFalse(self.read_client.body.closed.is_set())

    async def test_open_ended_range_bypasses_startup_cache(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=0-"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b"abcd")
        self.assertEqual(self.read_client.calls[0][1], ByteRange(0, 3))

    async def test_logout_and_cross_origin_mutations_are_rejected(self) -> None:
        cookie = await self._login()
        forbidden = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            headers={"Origin": "https://attacker.invalid"}, cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(forbidden.status, 403)
        logout = await self.client.post("/api/v1/auth/logout", cookies={"tgvio_player_session": cookie})
        self.assertEqual(logout.status, 200)
        self.assertEqual((await self.client.get(
            f"/api/v1/media/{self.media_id}", cookies={"tgvio_player_session": cookie}
        )).status, 401)

    async def test_proxy_forwarded_origin_is_accepted_behind_tls_termination(self) -> None:
        cookie = await self._login()
        headers = {
            "Origin": "https://csdn.im",
            "X-Forwarded-Host": "csdn.im",
            "X-Forwarded-Proto": "https",
        }
        ok = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            headers=headers,
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(ok.status, 200)
        # The mutation is idempotent and the favorites listing stays path-free.
        again = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            headers=headers,
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(again.status, 200)
        listed = await self.client.get(
            "/api/v1/favorites", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(listed.status, 200)
        body = await listed.json()
        self.assertEqual([item["id"] for item in body["items"]], [self.media_id])
        self.assertNotIn("remote", str(body))
        removed = await self.client.delete(
            f"/api/v1/media/{self.media_id}/favorite",
            headers=headers,
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(removed.status, 200)
        empty = await self.client.get(
            "/api/v1/favorites", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual((await empty.json())["items"], [])
        # A hostile Origin is still rejected even if a forwarded host is present.
        hostile = await self.client.put(
            f"/api/v1/media/{self.media_id}/favorite",
            headers={"Origin": "https://attacker.invalid", "X-Forwarded-Host": "csdn.im"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(hostile.status, 403)
        self.assertEqual(
            (await self.client.get("/api/v1/favorites")).status, 401
        )

    async def test_favorites_are_cursor_paged_with_stable_ties(self) -> None:
        cookie = await self._login()
        media_ids = [self.media_id, "c" * 64, "b" * 64]
        package = CatalogPackage(
            "favorite-page-package", "TGVIO/2026-09-22/2", "d" * 64,
            '"manifest-2"', '"complete-2"',
            tuple(CatalogMedia(media_id, "video", 4, "video/mp4", 1080, 1920, 2.0) for media_id in media_ids[1:]),
            tuple(CatalogLocation(media_id, "favorite-page-package", f"{media_id}.mp4") for media_id in media_ids[1:]),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        for media_id in media_ids:
            response = await self.client.put(
                f"/api/v1/media/{media_id}/favorite", cookies={"tgvio_player_session": cookie}
            )
            self.assertEqual(response.status, 200)
        # Use a shared timestamp to exercise the media ID tie-breaker.
        self.repo._require().execute(
            "UPDATE favorites SET created_at=123 WHERE token_digest=?", (token_digest(cookie),)
        )
        first = await self.client.get(
            "/api/v1/favorites?limit=1", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(first.status, 200)
        first_body = await first.json()
        self.assertTrue(first_body["has_more"])
        self.assertIsNotNone(first_body["next_cursor"])
        ids = [first_body["items"][0]["id"]]
        second = await self.client.get(
            "/api/v1/favorites?limit=1&cursor=" + first_body["next_cursor"],
            cookies={"tgvio_player_session": cookie},
        )
        second_body = await second.json()
        ids.extend(item["id"] for item in second_body["items"])
        third = await self.client.get(
            "/api/v1/favorites?limit=1&cursor=" + second_body["next_cursor"],
            cookies={"tgvio_player_session": cookie},
        )
        third_body = await third.json()
        ids.extend(item["id"] for item in third_body["items"])
        self.assertFalse(third_body["has_more"])
        self.assertEqual(ids, sorted(media_ids))
        self.assertNotIn("remote_path", str(first_body))
        self.assertEqual(
            (await self.client.get("/api/v1/favorites?cursor=bad", cookies={"tgvio_player_session": cookie})).status,
            400,
        )
        self.assertEqual((await self.client.get("/api/v1/favorites?limit=1")).status, 401)

    async def test_failed_logins_are_rate_limited_and_success_clears_failures(self) -> None:
        for _ in range(4):
            response = await self.client.post("/api/v1/auth/login", json={"secret": "wrong"})
            self.assertEqual(response.status, 401)
        self.assertEqual((await self.client.post(
            "/api/v1/auth/login", json={"secret": "s" * 32}
        )).status, 200)
        for _ in range(5):
            response = await self.client.post("/api/v1/auth/login", json={"secret": "wrong"})
            self.assertEqual(response.status, 401)
        blocked = await self.client.post("/api/v1/auth/login", json={"secret": "wrong"})
        self.assertEqual(blocked.status, 429)
        self.assertIn("Retry-After", blocked.headers)


    async def test_preload_requests_do_not_consume_playback_budget(self) -> None:
        # Keep room for a foreground slot while the preload and active playback
        # run together. With two slots, the new admission rule correctly rejects
        # this preload to preserve that final slot.
        self.server._max_streams = 3
        self.server._max_preload = 1
        self.server._stream_slots = asyncio.BoundedSemaphore(3)
        cookie = await self._login()
        stream = f"/api/v1/media/{self.media_id}/stream"
        hold = asyncio.Event()
        self.read_client.body = ClosableBody([b"hold"], wait=hold)
        first = asyncio.create_task(self.client.get(
            stream, headers={"Range": "bytes=2-3"}, cookies={"tgvio_player_session": cookie}
        ))
        await asyncio.sleep(0)
        blocked = await self.client.get(
            stream, headers={"Range": "bytes=2-3"}, cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(blocked.status, 429)

        preload_hold = asyncio.Event()
        self.read_client.body = ClosableBody([b"warm"], wait=preload_hold)
        preload = asyncio.create_task(self.client.get(
            stream,
            headers={"Range": "bytes=2-3", "X-TGVIO-Preload": "1"},
            cookies={"tgvio_player_session": cookie},
        ))
        await asyncio.sleep(0)
        second_preload = await self.client.get(
            stream,
            headers={"Range": "bytes=2-3", "X-TGVIO-Preload": "1"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(second_preload.status, 429)

        preload_hold.set()
        preload_response = await preload
        self.assertEqual(preload_response.status, 206)
        preload_response.close()
        hold.set()
        first_response = await first
        first_response.close()

    async def test_cache_headers_separate_api_and_assets(self) -> None:
        web_dir = Path(self.tmp.name) / "web"
        (web_dir / "assets").mkdir(parents=True)
        (web_dir / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
        (web_dir / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.read_client),
            static_dir=web_dir,
        )
        client = TestClient(TestServer(server.application()))
        await client.start_server()
        try:
            health = await client.get("/healthz")
            self.assertEqual(health.headers["Cache-Control"], "no-store")
            root = await client.get("/")
            self.assertEqual(root.headers["Cache-Control"], "no-cache")
            asset = await client.get("/assets/app.js")
            self.assertEqual(
                asset.headers["Cache-Control"], "public, max-age=31536000, immutable"
            )
            feed = await client.get("/api/v1/feed")
            self.assertEqual(feed.status, 401)
            self.assertEqual(feed.headers["Cache-Control"], "no-store")
        finally:
            await client.close()

    async def test_health_reports_saturated_playback_capacity(self) -> None:
        self.assertTrue(await self.server._acquire_stream("client-a"))
        self.assertTrue(await self.server._acquire_stream("client-b"))
        try:
            response = await self.client.get("/healthz")
            self.assertEqual(response.status, 200)
            self.assertEqual(
                (await response.json())["stream_capacity"],
                {
                    "limit": 2,
                    "active_playback": 2,
                    "active_preload": 0,
                    "active_probe": 0,
                    "active_cover": 0,
                    "cover_limit": self.server._max_cover,
                    "cover_mirror": {
                        "enabled": False,
                        "files": 0,
                        "bytes": 0,
                        "hits": 0,
                        "misses": 0,
                        "warm_pending": 0,
                        "warm_failed": 0,
                    },
                    "foreground_waiters": 0,
                    "available": 0,
                    "saturated": True,
                },
            )
        finally:
            await self.server._release_stream("client-a")
            await self.server._release_stream("client-b")

    async def test_public_pwa_assets_are_served_from_an_allowlist(self) -> None:
        static_dir = Path(self.tmp.name) / "pwa"
        static_dir.mkdir()
        names = (
            "site.webmanifest",
            "apple-touch-icon.png",
            "player-icon-192.png",
            "player-icon-512.png",
            "player-icon.svg",
        )
        for name in names:
            (static_dir / name).write_bytes(name.encode())
        server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.read_client),
            static_dir=static_dir,
        )
        client = TestClient(TestServer(server.application()))
        await client.start_server()
        try:
            for name in names:
                response = await client.get(f"/{name}")
                self.assertEqual(response.status, 200, name)
                self.assertEqual(await response.read(), name.encode())
            self.assertEqual((await client.get("/not-a-public-file.txt")).status, 404)
        finally:
            await client.close()

    # --- collections ---------------------------------------------------------

    @staticmethod
    def _collection_cookies(cookie: str) -> dict[str, str]:
        return {"tgvio_player_session": cookie}

    async def _create_collection(self, cookie: str, **payload) -> dict:
        body = {"name": "旅行", "kind": "manual"}
        body.update(payload)
        response = await self.client.post(
            "/api/v1/collections", json=body, cookies=self._collection_cookies(cookie)
        )
        self.assertEqual(response.status, 201, await response.text())
        return await response.json()

    async def _collection_rows(self, cookie: str) -> list[dict]:
        response = await self.client.get(
            "/api/v1/collections", cookies=self._collection_cookies(cookie)
        )
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["items"]

    async def test_collections_require_a_session(self) -> None:
        for method, path in (
            ("get", "/api/v1/collections"),
            ("post", "/api/v1/collections"),
            ("patch", "/api/v1/collections/whatever"),
            ("delete", "/api/v1/collections/whatever"),
            ("get", "/api/v1/collections/whatever/items"),
            ("put", f"/api/v1/collections/whatever/items/{self.media_id}"),
            ("delete", f"/api/v1/collections/whatever/items/{self.media_id}"),
        ):
            response = await getattr(self.client, method)(path)
            self.assertEqual(response.status, 401, path)

    async def test_builtin_favorites_is_first_and_cannot_be_renamed_or_deleted(self) -> None:
        cookie = await self._login()
        first = (await self._collection_rows(cookie))[0]
        self.assertEqual(
            {key: first[key] for key in ("collection_id", "kind", "name")},
            {"collection_id": "favorites", "kind": "builtin", "name": "收藏"},
        )
        for method, payload in (("patch", {"name": "x"}), ("delete", None)):
            kwargs: dict = {"cookies": self._collection_cookies(cookie)}
            if payload is not None:
                kwargs["json"] = payload
            response = await getattr(self.client, method)(
                "/api/v1/collections/favorites", **kwargs
            )
            self.assertEqual(response.status, 400)

    async def test_member_add_and_remove_are_idempotent(self) -> None:
        cookie = await self._login()
        created = await self._create_collection(cookie)
        path = f"/api/v1/collections/{created['collection_id']}/items/{self.media_id}"
        for _ in range(2):
            self.assertEqual(
                (await self.client.put(path, cookies=self._collection_cookies(cookie))).status, 204
            )
        for _ in range(2):
            self.assertEqual(
                (await self.client.delete(path, cookies=self._collection_cookies(cookie))).status, 204
            )

    async def test_a_member_appears_only_after_it_is_added(self) -> None:
        cookie = await self._login()
        created = await self._create_collection(cookie)
        base = f"/api/v1/collections/{created['collection_id']}/items"
        empty = await (await self.client.get(base, cookies=self._collection_cookies(cookie))).json()
        self.assertEqual(empty, {"items": [], "has_more": False, "next_cursor": None})
        await self.client.put(f"{base}/{self.media_id}", cookies=self._collection_cookies(cookie))
        page = await (await self.client.get(base, cookies=self._collection_cookies(cookie))).json()
        self.assertEqual([item["id"] for item in page["items"]], [self.media_id])
        row = [
            item for item in await self._collection_rows(cookie)
            if item["collection_id"] == created["collection_id"]
        ][0]
        self.assertEqual((row["name"], row["count"], row["count_capped"]), ("旅行", 1, False))

    async def test_a_collection_can_be_reordered_over_http(self) -> None:
        cookie = await self._login()
        cookies = self._collection_cookies(cookie)
        first = await self._create_collection(cookie, name="第一")
        second = await self._create_collection(cookie, name="第二")
        self.assertEqual(
            [row["name"] for row in await self._collection_rows(cookie)][1:], ["第一", "第二"]
        )
        moved = await self.client.patch(
            f"/api/v1/collections/{second['collection_id']}",
            json={"sort_order": -1}, cookies=cookies,
        )
        self.assertEqual(moved.status, 200, await moved.text())
        self.assertEqual(
            [row["name"] for row in await self._collection_rows(cookie)][1:], ["第二", "第一"]
        )
        for body in ({"sort_order": "1"}, {"sort_order": True}, {"sort_order": 1.5}):
            response = await self.client.patch(
                f"/api/v1/collections/{first['collection_id']}", json=body, cookies=cookies
            )
            self.assertEqual(response.status, 400, body)

    async def test_collection_items_never_hand_out_a_cursor_past_the_budget(self) -> None:
        cookie = await self._login()
        cookies = self._collection_cookies(cookie)
        others = ("d" * 64, "e" * 64)
        await self.repo.apply_package(CatalogPackage(
            "package3", "TGVIO/2026-10-05/1", "f" * 64, '"m3"', '"c3"',
            tuple(CatalogMedia(item, "video", 4, "video/mp4", 1080, 1920, 3.0) for item in others),
            tuple(CatalogLocation(item, "package3", f"{item[0]}.mp4", '"etag3"') for item in others),
        ))
        await self.repo.refresh_media_activity()
        created = await self._create_collection(cookie)
        base = f"/api/v1/collections/{created['collection_id']}/items"
        for media_id in (self.media_id, *others):
            await self.client.put(f"{base}/{media_id}", cookies=cookies)
        with patch("tgvio_player.adapters.http.library._MAX_ITEMS_OFFSET", 1):
            first = await (await self.client.get(f"{base}?limit=1", cookies=cookies)).json()
            self.assertEqual(first["next_cursor"], "1")
            second = await (await self.client.get(f"{base}?limit=1&cursor=1", cookies=cookies)).json()
        self.assertTrue(second["has_more"], "there is still a member behind it")
        self.assertIsNone(second["next_cursor"], "and no cursor past the budget to ask for it")

    async def test_the_builtin_count_is_capped_without_paging(self) -> None:
        cookie = await self._login()
        await self.repo.set_favorite(token_digest(cookie), self.media_id, enabled=True)
        self.assertEqual((await self._collection_rows(cookie))[0]["count"], 1)
        with patch("tgvio_player.adapters.http.library._MAX_COLLECTION_COUNT", 0):
            capped = (await self._collection_rows(cookie))[0]
        self.assertEqual((capped["count"], capped["count_capped"]), (0, True))

    async def test_collection_items_can_be_narrowed_by_the_same_filters(self) -> None:
        cookie = await self._login()
        cookies = self._collection_cookies(cookie)
        created = await self._create_collection(cookie)
        base = f"/api/v1/collections/{created['collection_id']}/items"
        await self.client.put(f"{base}/{self.media_id}", cookies=cookies)
        body = await (await self.client.get(f"{base}?min_seconds=1", cookies=cookies)).json()
        self.assertEqual([item["id"] for item in body["items"]], [self.media_id])
        narrowed = await (await self.client.get(f"{base}?min_seconds=100", cookies=cookies)).json()
        self.assertEqual(narrowed["items"], [])
        self.assertIsNone(narrowed["next_cursor"])
        for query in ("colour=red", "min_seconds=abc", "sort=random"):
            response = await self.client.get(f"{base}?{query}", cookies=cookies)
            self.assertEqual(response.status, 400, query)

    async def test_a_smart_collection_is_not_filtered_by_the_panel(self) -> None:
        cookie = await self._login()
        cookies = self._collection_cookies(cookie)
        created = await self._create_collection(
            cookie, name="短片", kind="smart", rules_json='{"min_seconds": 1}'
        )
        base = f"/api/v1/collections/{created['collection_id']}/items"
        response = await self.client.get(f"{base}?min_seconds=1", cookies=cookies)
        self.assertEqual(
            response.status, 400,
            "a smart collection's conditions are its filters; a second set is a client bug",
        )

    async def test_unknown_collections_and_bad_names_are_rejected(self) -> None:
        cookie = await self._login()
        cookies = self._collection_cookies(cookie)
        self.assertEqual(
            (await self.client.get("/api/v1/collections/missing/items", cookies=cookies)).status, 404
        )
        self.assertEqual(
            (await self.client.put(
                f"/api/v1/collections/missing/items/{self.media_id}", cookies=cookies
            )).status, 404
        )
        self.assertEqual(
            (await self.client.patch(
                "/api/v1/collections/missing", json={"name": "x"}, cookies=cookies
            )).status, 404
        )
        self.assertEqual(
            (await self.client.delete("/api/v1/collections/missing", cookies=cookies)).status, 404
        )
        for body in (
            {"name": "", "kind": "manual"},
            {"name": "   ", "kind": "manual"},
            {"name": "x" * 61, "kind": "manual"},
            {"name": "旅行", "kind": "builtin"},
            {"name": "旅行", "kind": "manual", "colour": "red"},
            {"name": "旅行", "kind": "smart", "rules_json": "{"},
            {"name": "旅行", "kind": "smart", "rules_json": "[]"},
        ):
            response = await self.client.post("/api/v1/collections", json=body, cookies=cookies)
            self.assertEqual(response.status, 400, body)
        created = await self._create_collection(cookie)
        self.assertEqual(
            (await self.client.patch(
                f"/api/v1/collections/{created['collection_id']}",
                json={"colour": "red"}, cookies=cookies,
            )).status, 400
        )
        renamed = await self.client.patch(
            f"/api/v1/collections/{created['collection_id']}",
            json={"name": "重看"}, cookies=cookies,
        )
        self.assertEqual(renamed.status, 200)
        row = [
            item for item in await self._collection_rows(cookie)
            if item["collection_id"] == created["collection_id"]
        ][0]
        self.assertEqual(row["name"], "重看")
        self.assertEqual(
            (await self.client.delete(
                f"/api/v1/collections/{created['collection_id']}", cookies=cookies
            )).status, 204
        )
        self.assertEqual(len(await self._collection_rows(cookie)), 1, "only the builtin is left")

    async def test_smart_collections_evaluate_their_rules(self) -> None:
        cookie = await self._login()
        cookies = self._collection_cookies(cookie)
        # The fixture video is two seconds long.
        matching = await self._create_collection(
            cookie, name="短片", kind="smart", rules_json='{"min_seconds": 1}'
        )
        missing = await self._create_collection(
            cookie, name="无", kind="smart", rules_json='{"min_seconds": 100}'
        )
        page = await (await self.client.get(
            f"/api/v1/collections/{matching['collection_id']}/items", cookies=cookies
        )).json()
        self.assertEqual([item["id"] for item in page["items"]], [self.media_id])
        empty = await (await self.client.get(
            f"/api/v1/collections/{missing['collection_id']}/items", cookies=cookies
        )).json()
        self.assertEqual(empty["items"], [])
        rows = {item["collection_id"]: item for item in await self._collection_rows(cookie)}
        self.assertEqual((rows[matching["collection_id"]]["kind"], rows[matching["collection_id"]]["count"]), ("smart", 1))
        self.assertEqual(rows[missing["collection_id"]]["count"], 0)

    async def test_unreadable_smart_rules_select_nothing_never_the_whole_library(self) -> None:
        """Review Focus: a corrupt or empty rule blob is an empty collection.

        The API refuses to *store* rules it cannot read back, so the unreadable
        blobs are written straight to the table - that is how a build newer than
        this one, or a restored payload, leaves a row behind.
        """
        cookie = await self._login()
        cookies = self._collection_cookies(cookie)
        for rules in (None, "{}", '{"nope": 1}'):
            created = await self._create_collection(cookie, name="空", kind="smart", rules_json=rules)
            await self._assert_empty_smart(cookie, cookies, created["collection_id"], repr(rules))
        connection = self.repo._require()
        for raw in ("", "{", "[]", '"text"'):
            connection.execute(
                "INSERT INTO collections(collection_id, name, kind, rules_json) VALUES(?,?,?,?)",
                ("c" * 8 + str(len(raw)), "坏", "smart", raw),
            )
            connection.commit()
            await self._assert_empty_smart(
                cookie, cookies, "c" * 8 + str(len(raw)), repr(raw)
            )

    async def _assert_empty_smart(
        self, cookie: str, cookies: dict[str, str], collection_id: str, label: str
    ) -> None:
        page = await (await self.client.get(
            f"/api/v1/collections/{collection_id}/items", cookies=cookies
        )).json()
        self.assertEqual(page["items"], [], label)
        rows = {item["collection_id"]: item for item in await self._collection_rows(cookie)}
        self.assertEqual(rows[collection_id]["count"], 0, label)

    async def test_a_smart_count_is_bounded_and_says_when_it_hit_the_bound(self) -> None:
        second = "d" * 64
        await self.repo.apply_package(CatalogPackage(
            "package2", "TGVIO/2026-09-23/1", "c" * 64, '"manifest2"', '"complete2"',
            (CatalogMedia(second, "video", 8, "video/mp4", 1080, 1920, 3.0),),
            (CatalogLocation(second, "package2", "second.mp4", '"etag2"'),),
        ))
        await self.repo.refresh_media_activity()
        cookie = await self._login()
        created = await self._create_collection(
            cookie, name="全部", kind="smart", rules_json='{"min_seconds": 1}'
        )

        async def count_for(collection_id: str) -> tuple[int, bool]:
            row = [
                item for item in await self._collection_rows(cookie)
                if item["collection_id"] == collection_id
            ][0]
            return row["count"], row["count_capped"]

        self.assertEqual(await count_for(created["collection_id"]), (2, False))
        with patch("tgvio_player.adapters.http.library._MAX_COLLECTION_COUNT", 1):
            self.assertEqual(await count_for(created["collection_id"]), (1, True))

    async def test_builtin_favorites_lists_the_same_favorites_as_the_page(self) -> None:
        cookie = await self._login()
        cookies = self._collection_cookies(cookie)
        await self.repo.set_favorite(token_digest(cookie), self.media_id, enabled=True)
        first = (await self._collection_rows(cookie))[0]
        self.assertEqual((first["collection_id"], first["count"]), ("favorites", 1))
        page = await (await self.client.get(
            "/api/v1/collections/favorites/items", cookies=cookies
        )).json()
        self.assertEqual([item["id"] for item in page["items"]], [self.media_id])
        # Read-only through the member routes too: favourites are written on the media.
        for method in ("put", "delete"):
            self.assertEqual(
                (await getattr(self.client, method)(
                    f"/api/v1/collections/favorites/items/{self.media_id}", cookies=cookies
                )).status, 400
            )

    async def test_builtin_favorites_follows_the_backup_chain_when_it_is_configured(self) -> None:
        """The same authority as the favourites page, in the other runtime mode."""
        await self._enable_storage_services()
        cookie = await self._login()
        cookies = self._collection_cookies(cookie)
        await self.repo.set_global_favorite(self.media_id, True)
        first = (await self._collection_rows(cookie))[0]
        self.assertEqual((first["collection_id"], first["count"]), ("favorites", 1))
        page = await (await self.client.get(
            "/api/v1/collections/favorites/items", cookies=cookies
        )).json()
        self.assertEqual([item["id"] for item in page["items"]], [self.media_id])


class ClientIdentityTests(unittest.TestCase):
    def test_resolve_client_behind_trusted_proxy(self) -> None:
        class FakeRequest:
            def __init__(self, remote: str | None, headers: dict[str, str]) -> None:
                self.remote = remote
                self.headers = headers

        self.assertEqual(
            resolve_client(FakeRequest("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})),
            "203.0.113.9",
        )
        self.assertEqual(
            resolve_client(FakeRequest("172.20.0.1", {"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})),
            "198.51.100.7",
        )
        self.assertEqual(
            resolve_client(FakeRequest("127.0.0.1", {"X-Real-IP": "198.51.100.9"})),
            "198.51.100.9",
        )
        self.assertEqual(resolve_client(FakeRequest("172.20.0.1", {})), "172.20.0.1")
        self.assertEqual(resolve_client(FakeRequest(None, {})), "unknown")


class PlayerCoverRouteTests(unittest.IsolatedAsyncioTestCase):
    """Archive covers are served from their own tiny budget, never a playback slot.

    A cover grid must be able to render while every playback slot is busy: one
    client's browsing can never be the reason another client cannot watch.
    """

    JPEG = b"\xff\xd8\xff\xe0cover-bytes"

    class FakeCoverReader:
        def __init__(self, payload: bytes, status: int = 206) -> None:
            self.payload = payload
            self.status = status
            self.calls: list[tuple[str, ByteRange | None]] = []

        async def open_range(self, remote_path: str, byte_range: ByteRange | None) -> WebDavRangeResponse:
            self.calls.append((remote_path, byte_range))
            length = len(self.payload) if byte_range is None else byte_range.length
            return WebDavRangeResponse(
                self.status, "image/jpeg", length, None, '"cover"', ClosableBody([self.payload])
            )

    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.media_id = "a" * 64
        self.plain_id = "9" * 64
        package = CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, '"manifest"', '"complete"',
            (
                CatalogMedia(self.media_id, "video", 8, "video/mp4", 1080, 1920, 2.0),
                CatalogMedia(self.plain_id, "video", 8, "video/mp4", 1080, 1920, 2.0),
            ),
            (
                CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),
                CatalogLocation(self.plain_id, "package", "plain.mp4", '"etag"'),
            ),
            (
                CatalogCover(
                    media_id=self.media_id, package_id="package",
                    remote_relpath="cover/video-cover.jpg", size_bytes=len(self.JPEG),
                    mime_type="image/jpeg", algorithm="reuse-publish-thumbnail-v1",
                ),
            ),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        self.reader = self.FakeCoverReader(self.JPEG)
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            # The production shape: the read-only adapter assembles and validates
            # the catalog-owned path before the transport sees it.
            ReadOnlyWebDavAdapter(self.reader),
            max_streams=4,
            max_streams_per_client=4,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def _login(self) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.assertEqual(response.status, 200)
        return response.cookies["tgvio_player_session"].value

    async def _cover(self, cookie: str | None, media_id: str | None = None):
        kwargs = {} if cookie is None else {"cookies": {"tgvio_player_session": cookie}}
        return await self.client.get(f"/api/v1/media/{media_id or self.media_id}/cover", **kwargs)

    async def test_cover_requires_the_player_session(self) -> None:
        response = await self._cover(None)
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_cover_is_served_as_a_bounded_private_image(self) -> None:
        cookie = await self._login()
        cover = await self.repo.active_cover(self.media_id)
        response = await self.client.get(f"/api/v1/media/{self.media_id}/cover?v={cover['version']}",
                                         cookies={"tgvio_player_session": cookie})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=3600")
        self.assertIn("Cookie", response.headers["Vary"])
        self.assertEqual(await response.read(), self.JPEG)
        self.assertEqual(
            self.reader.calls, [("TGVIO/2026-09-22/1/cover/video-cover.jpg", ByteRange(0, len(self.JPEG) - 1))]
        )

    async def test_cover_errors_and_unversioned_images_are_never_cached(self) -> None:
        cookie = await self._login()
        response = await self._cover(cookie)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for status in (404, 503):
            self.reader.status = status
            response = await self._cover(cookie)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_cover_stops_reading_as_soon_as_the_declared_image_is_sent(self) -> None:
        cookie = await self._login()
        body = ClosableBody([self.JPEG, b"unbounded-tail", b"must-not-read"])
        self.reader.open_range = AsyncMock(return_value=WebDavRangeResponse(
            200, "image/jpeg", None, None, None, body,
        ))
        response = await self._cover(cookie)
        self.assertEqual(await response.read(), self.JPEG)
        await asyncio.wait_for(body.closed.wait(), timeout=1)
        self.assertEqual(len(body._chunks), 2, "close the upstream at the cap, do not drain its tail")
        self.assertEqual(self.server._cover_active, 0)

    async def test_cover_prepare_failure_closes_upstream_and_releases_budget(self) -> None:
        body = ClosableBody([self.JPEG])
        self.reader.open_range = AsyncMock(return_value=WebDavRangeResponse(
            206, "image/jpeg", len(self.JPEG), None, None, body,
        ))
        self.server._authenticate = AsyncMock(return_value="test-session")
        request = type("CoverRequest", (dict,), {})(player_request_id="test")
        request.match_info = {"media_id": self.media_id}
        request.query = {}
        with patch("aiohttp.web.StreamResponse.prepare", side_effect=ConnectionResetError):
            with self.assertRaises(ConnectionResetError):
                await self.server._cover(request)
        self.assertTrue(body.closed.is_set(), "prepare failure must not leak the upstream")
        self.assertEqual(self.server._cover_active, 0)

    async def test_cover_bytes_are_capped_by_the_catalog_declaration(self) -> None:
        cookie = await self._login()
        await self.repo.apply_package(CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, '"manifest"', '"complete"',
            (
                CatalogMedia(self.media_id, "video", 8, "video/mp4", 1080, 1920, 2.0),
                CatalogMedia(self.plain_id, "video", 8, "video/mp4", 1080, 1920, 2.0),
            ),
            (
                CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),
                CatalogLocation(self.plain_id, "package", "plain.mp4", '"etag"'),
            ),
            (
                CatalogCover(
                    media_id=self.media_id, package_id="package",
                    remote_relpath="cover/video-cover.jpg", size_bytes=6,
                    mime_type="image/jpeg", algorithm="reuse-publish-thumbnail-v1",
                ),
            ),
        ))
        response = await self._cover(cookie)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.JPEG[:6], "a cover can never stream past its declared size")

    async def test_unknown_media_or_missing_cover_is_not_found(self) -> None:
        cookie = await self._login()
        self.assertEqual((await self._cover(cookie, "f" * 64)).status, 404)
        self.assertEqual((await self._cover(cookie, self.plain_id)).status, 404)
        self.assertEqual(self.reader.calls, [], "no upstream read without a catalog cover row")

    async def test_cover_never_consumes_a_playback_slot(self) -> None:
        cookie = await self._login()
        held = [await self.server._acquire_stream(f"client-{index}") for index in range(4)]
        self.assertTrue(all(held))
        self.assertTrue(self.server.playback_saturated, "precondition: every playback slot is taken")
        try:
            response = await self._cover(cookie)
            self.assertEqual(response.status, 200, "covers must never compete with playback")
            self.assertEqual(await response.read(), self.JPEG)
            self.assertEqual(self.server.active_playback_streams, 4)
            self.assertTrue(self.server.playback_saturated, "a cover must not release a playback slot")
        finally:
            for index in range(4):
                await self.server._release_stream(f"client-{index}")


    async def test_the_dto_carries_a_fingerprint_only_when_the_cover_has_one(self) -> None:
        cookie = await self._login()
        cookies = {"tgvio_player_session": cookie}
        with_cover = await (await self.client.get(f"/api/v1/media/{self.media_id}", cookies=cookies)).json()
        self.assertNotIn("phash", with_cover, "a cover without a fingerprint says nothing")
        self.assertNotIn(
            "phash",
            await (await self.client.get(f"/api/v1/media/{self.plain_id}", cookies=cookies)).json(),
            "no cover, no fingerprint",
        )
        self.assertEqual(
            (await self.client.get(f"/api/v1/media/{self.media_id}")).status, 401,
            "the fingerprint needs a session, like the cover it belongs to",
        )

    async def test_the_policy_allows_the_inline_grain_the_design_uses(self) -> None:
        # The foil surface paints a 2% grain from a data: URI on purpose, so the app never
        # requests an asset for it. Without img-src the browser blocks it, the texture is
        # missing and the console fills with violations.
        response = await self.client.get("/healthz")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("img-src 'self' data:", csp)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)

    async def test_a_retired_cover_row_never_leaks_its_fingerprint(self) -> None:
        # The same video carries two covers; the retired package's row must not decide
        # what the DTO reports.
        await self.repo.apply_package(CatalogPackage(
            "second", "TGVIO/2026-09-23/1", "c" * 64, '"m2"', '"c2"',
            (CatalogMedia(self.media_id, "video", 8, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "second", "video.mp4", '"etag2"'),),
            (
                CatalogCover(
                    media_id=self.media_id, package_id="second",
                    remote_relpath="cover/second-cover.jpg", size_bytes=len(self.JPEG),
                    mime_type="image/jpeg", algorithm="bounded-frame-v2",
                    phash="0123456789abcdef",
                ),
            ),
        ))
        cookie = await self._login()
        cookies = {"tgvio_player_session": cookie}
        # The first package's cover is the active one, and it carries no fingerprint.
        before = await (await self.client.get(f"/api/v1/media/{self.media_id}", cookies=cookies)).json()
        self.assertNotIn("phash", before)
        # Retire it: the hashed cover becomes the active one, so its fingerprint is served.
        await self.repo.record_deleted_cover(self.media_id, "package")
        active = await (await self.client.get(f"/api/v1/media/{self.media_id}", cookies=cookies)).json()
        self.assertEqual(
            active.get("phash"), "0123456789abcdef",
            "the active cover's fingerprint is the one served",
        )
        # Retire that one too: no active cover, so no fingerprint and no cover url.
        await self.repo.record_deleted_cover(self.media_id, "second")
        after = await (await self.client.get(f"/api/v1/media/{self.media_id}", cookies=cookies)).json()
        self.assertNotIn(
            "phash", after,
            "a retired cover row must not keep serving its fingerprint",
        )
        self.assertIsNone(after.get("cover_url"))

    async def test_cover_budget_is_bounded_and_fails_fast(self) -> None:
        cookie = await self._login()
        acquired = 0
        while await self.server._acquire_cover(diagnostics={}):
            acquired += 1
            if acquired > 64:
                self.fail("cover budget is unbounded")
        self.assertGreaterEqual(acquired, 1)
        try:
            response = await self._cover(cookie)
            self.assertEqual(response.status, 503, "a saturated cover budget must fail fast, not queue")
        finally:
            for _ in range(acquired):
                await self.server._release_cover()
        self.assertEqual((await self._cover(cookie)).status, 200)

    async def test_upstream_failures_map_to_clear_status_codes(self) -> None:
        cookie = await self._login()
        self.reader.status = 404
        self.assertEqual((await self._cover(cookie)).status, 404)
        self.reader.status = 503
        self.assertEqual((await self._cover(cookie)).status, 502)

    async def test_short_upstream_cover_fails_instead_of_stalling_the_client(self) -> None:
        cookie = await self._login()
        self.reader.payload = b""
        response = await self._cover(cookie)
        with self.assertRaises(Exception) as caught:
            await asyncio.wait_for(response.read(), timeout=3)
        self.assertNotIsInstance(
            caught.exception, asyncio.TimeoutError,
            "a short cover upstream stalled the client instead of failing the response",
        )

    async def test_cover_open_failure_is_a_clean_upstream_error(self) -> None:
        cookie = await self._login()
        self.reader.open_range = AsyncMock(side_effect=ConnectionError("fixture unavailable"))
        response = await self._cover(cookie)
        self.assertEqual(response.status, 502)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.server._cover_active, 0)

    async def test_stale_cover_version_is_rejected_without_reading_archive(self) -> None:
        cookie = await self._login()
        response = await self.client.get(f"/api/v1/media/{self.media_id}/cover?v=stale",
                                         cookies={"tgvio_player_session": cookie})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.reader.calls, [])

    async def test_new_cover_package_has_a_new_url_without_rewriting_old_manifest(self) -> None:
        cookie = await self._login()
        before = await self.repo.active_cover(self.media_id)
        await self.repo.apply_package(CatalogPackage(
            "a-new-cover-package", "TGVIO/2026-09-23/2", "c" * 64, None, None,
            (CatalogMedia(self.media_id, "video", 8, "video/mp4"),),
            (CatalogLocation(self.media_id, "a-new-cover-package", "video.mp4"),),
            (CatalogCover(self.media_id, "a-new-cover-package", "cover/new.jpg", len(self.JPEG),
                          "image/jpeg", "reuse-publish-thumbnail-v1"),),
        ))
        after = await self.repo.active_cover(self.media_id)
        self.assertNotEqual(before["version"], after["version"])
        response = await self.client.get(f"/api/v1/media/{self.media_id}/cover?v={before['version']}",
                                         cookies={"tgvio_player_session": cookie})
        self.assertEqual(response.status, 404)
        self.assertEqual(self.reader.calls, [])

    async def test_media_dto_advertises_a_cover_only_when_one_exists(self) -> None:
        cookie = await self._login()
        cover = await self.repo.active_cover(self.media_id)
        cover_url = f"/api/v1/media/{self.media_id}/cover?v={cover['version']}"
        with_cover = await self.client.get(
            f"/api/v1/media/{self.media_id}", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual((await with_cover.json())["cover_url"], cover_url)
        without_cover = await self.client.get(
            f"/api/v1/media/{self.plain_id}", cookies={"tgvio_player_session": cookie}
        )
        self.assertIsNone((await without_cover.json())["cover_url"])
        listing = await self.client.get("/api/v1/videos?category=all&limit=5&offset=0", cookies={"tgvio_player_session": cookie})
        self.assertEqual(
            {item["id"]: item["cover_url"] for item in (await listing.json())["items"]},
            {self.media_id: cover_url, self.plain_id: None},
        )

    async def test_health_reports_the_cover_budget(self) -> None:
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["stream_capacity"]["active_cover"], 0)
        # Covers run from their own pool, so the active count alone cannot tell an
        # operator whether covers are being shed. The ceiling has to be visible.
        self.assertEqual(
            (await response.json())["stream_capacity"]["cover_limit"],
            self.server._max_cover,
        )

    async def test_cover_ceiling_never_sheds_a_single_browse_page(self) -> None:
        # One browse page opens six cover lanes
        # (player/web/src/components/cover-load-queue.ts). The previous ceiling of
        # max(2, max_streams // 4) resolved to 2 here and shed the rest as 503, and
        # an <img> error carries no status, so a healthy grid painted
        # "封面加载失败" instead of waiting its turn.
        self.assertGreaterEqual(self.server._max_cover, 6)


class VideoCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.short_id = "1" * 64
        self.long_id = "2" * 64
        package = CatalogPackage(
            "package",
            "TGVIO/2026-09-22/1",
            "b" * 64,
            '"manifest"',
            '"complete"',
            (
                CatalogMedia(self.short_id, "video", 1000, "video/mp4", 1080, 1920, 12.0, codec="h264"),
                CatalogMedia(self.long_id, "video", 5000, "video/mp4", 1080, 1920, 900.0, codec="h264"),
            ),
            (
                CatalogLocation(self.short_id, "package", "short.mp4", '"etag"'),
                CatalogLocation(self.long_id, "package", "long.mp4", '"etag"'),
            ),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()

        class DummyReader:
            async def open_range(self, remote_path, byte_range):
                raise AssertionError("streaming is not expected in this test")

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

    async def test_long_and_short_categories(self) -> None:
        cookie = await self._login()
        long_items = await self.client.get(
            "/api/v1/videos?category=long", cookies={"tgvio_player_session": cookie}
        )
        body = await long_items.json()
        self.assertEqual([item["id"] for item in body["items"]], [self.long_id])
        self.assertEqual(body["items"][0]["category"], "long")
        self.assertEqual(body["items"][0]["size_bytes"], 5000)
        short_items = await self.client.get(
            "/api/v1/videos?category=short", cookies={"tgvio_player_session": cookie}
        )
        body = await short_items.json()
        self.assertEqual([item["id"] for item in body["items"]], [self.short_id])
        self.assertEqual(body["items"][0]["category"], "short")

    async def test_cache_query_adds_prefetch_to_stream_url(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            "/api/v1/videos?category=long&cache=1", cookies={"tgvio_player_session": cookie}
        )
        item = (await response.json())["items"][0]
        self.assertTrue(item["stream_url"].endswith("?cache=1"))

    async def test_short_feed_excludes_large_videos(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            "/api/v1/feed?limit=20", cookies={"tgvio_player_session": cookie}
        )
        ids = [item["id"] for item in (await response.json())["items"]]
        self.assertEqual(ids, [self.short_id])

    @staticmethod
    def _group_id(name: str) -> str:
        return base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")

    async def test_media_and_group_pages_follow_archive_date_groups(self) -> None:
        sibling_id = "3" * 64
        sibling_package = CatalogPackage(
            "sibling-package",
            "TGVIO/2026-09-22/2",
            "c" * 64,
            '"manifest-2"',
            '"complete-2"',
            (
                CatalogMedia(self.short_id, "video", 1000, "video/mp4", 1080, 1920, 12.0),
                CatalogMedia(sibling_id, "video", 2000, "video/mp4", 1080, 1920, 20.0),
            ),
            (
                CatalogLocation(self.short_id, "sibling-package", "duplicate.mp4", '"etag-2"'),
                CatalogLocation(sibling_id, "sibling-package", "sibling.mp4", '"etag-2"'),
            ),
        )
        another_date_package = CatalogPackage(
            "another-date-package",
            "TGVIO/2026-09-23/1",
            "d" * 64,
            '"manifest-3"',
            '"complete-3"',
            (CatalogMedia(self.short_id, "video", 1000, "video/mp4", 1080, 1920, 12.0),),
            (CatalogLocation(self.short_id, "another-date-package", "same-video.mp4", '"etag-3"'),),
        )
        await self.repo.apply_package(sibling_package)
        await self.repo.apply_package(another_date_package)
        await self.repo.refresh_media_activity()
        cookie = await self._login()

        details = await self.client.get(
            f"/api/v1/media/{self.short_id}", cookies={"tgvio_player_session": cookie}
        )
        details_body = await details.json()
        self.assertEqual(
            {group["label"] for group in details_body["groups"]},
            {"2026-09-22", "2026-09-23"},
        )
        self.assertNotIn("remote_path", str(details_body))

        group_id = self._group_id("2026-09-22")
        first_page = await self.client.get(
            f"/api/v1/groups/{group_id}/videos?limit=2",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(first_page.status, 200)
        first_body = await first_page.json()
        self.assertEqual(first_body["group"], {"id": group_id, "label": "2026-09-22"})
        first_ids = [item["id"] for item in first_body["items"]]
        self.assertEqual(len(first_ids), 2)
        self.assertEqual(len(set(first_ids)), 2)
        self.assertTrue(first_body["has_more"])
        self.assertNotIn("remote_path", str(first_body))

        second_page = await self.client.get(
            f"/api/v1/groups/{group_id}/videos?limit=2&cursor={first_body['next_cursor']}",
            cookies={"tgvio_player_session": cookie},
        )
        second_body = await second_page.json()
        all_ids = first_ids + [item["id"] for item in second_body["items"]]
        self.assertEqual(set(all_ids), {self.short_id, self.long_id, sibling_id})
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertFalse(second_body["has_more"])
        self.assertEqual(
            {item["category"] for item in first_body["items"] + second_body["items"]},
            {"short", "long"},
        )

        another_date_id = self._group_id("2026-09-23")
        another_date = await self.client.get(
            f"/api/v1/groups/{another_date_id}/videos?limit=20",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual([item["id"] for item in (await another_date.json())["items"]], [self.short_id])

    async def test_group_feed_authentication_and_validation(self) -> None:
        group_id = self._group_id("2026-09-22")
        unauthenticated = await self.client.get(f"/api/v1/groups/{group_id}/videos")
        self.assertEqual(unauthenticated.status, 401)
        cookie = await self._login()
        unknown = await self.client.get(
            f"/api/v1/groups/{self._group_id('2026-09-24')}/videos",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(unknown.status, 404)
        malformed = await self.client.get(
            "/api/v1/groups/!!!/videos", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(malformed.status, 400)
        bad_cursor = await self.client.get(
            f"/api/v1/groups/{group_id}/videos?cursor=not-a-media-id",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(bad_cursor.status, 400)

    async def test_long_video_progress_is_shared_between_authenticated_sessions(self) -> None:
        first_cookie = await self._login()
        second_cookie = await self._login()
        saved = await self.client.put(
            f"/api/v1/media/{self.long_id}/progress",
            json={"position_seconds": 412.5},
            cookies={"tgvio_player_session": first_cookie},
        )
        self.assertEqual(saved.status, 200)
        listed = await self.client.get(
            "/api/v1/long-progress",
            cookies={"tgvio_player_session": second_cookie},
        )
        self.assertEqual(listed.status, 200)
        self.assertEqual(
            (await listed.json())["items"],
            [{"id": self.long_id, "position_seconds": 412.5}],
        )

    async def test_short_video_cannot_be_added_to_long_resume_progress(self) -> None:
        cookie = await self._login()
        response = await self.client.put(
            f"/api/v1/media/{self.short_id}/progress",
            json={"position_seconds": 4},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 404)

    async def test_invalid_category_is_rejected(self) -> None:
        cookie = await self._login()
        response = await self.client.get(
            "/api/v1/videos?category=medium", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(response.status, 400)


class RangeCacheHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.buffer = bytes((index * 7) % 251 for index in range(200_000))
        self.media_id = "9" * 64
        package = CatalogPackage(
            "package",
            "TGVIO/2026-09-22/1",
            "b" * 64,
            '"manifest"',
            '"complete"',
            (CatalogMedia(self.media_id, "video", len(self.buffer), "video/mp4", 1080, 1920, 20.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()

        class BufferReader:
            def __init__(self, buffer: bytes) -> None:
                self.buffer = buffer
                self.calls: list[tuple[int, int]] = []

            async def open_range(self, package: str, relpath: str, byte_range: ByteRange):
                self.calls.append((byte_range.start, byte_range.end))
                payload = self.buffer[byte_range.start : byte_range.end + 1]

                class Body:
                    def __init__(self, data: bytes) -> None:
                        self.data = data
                        self.done = False

                    def __aiter__(self):
                        return self

                    async def __anext__(self) -> bytes:
                        if self.done:
                            raise StopAsyncIteration
                        self.done = True
                        return self.data

                    async def aclose(self) -> None:
                        return None

                class Response:
                    status = 206
                    content_length = len(payload)
                    body = Body(payload)

                return Response()

        from tgvio_player.application.range_cache import MediaRangeCache
        from tgvio_player.infrastructure.range_store import RangeStore

        self.reader = BufferReader(self.buffer)
        cache = MediaRangeCache(
            RangeStore(
                Path(self.tmp.name) / "cache", chunk_bytes=64 * 1024, max_bytes=8 * 64 * 1024
            ),
            self.reader,
        )
        cache.open()
        self.cache = cache
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            self.reader,
            range_cache=cache,
            warm_tail_bytes=64 * 1024,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.cache.shutdown()
        await self.repo.close()
        self.tmp.cleanup()

    async def _login(self) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.assertEqual(response.status, 200)
        return response.cookies["tgvio_player_session"].value

    async def test_first_range_fetches_chunks_and_second_reuses_cache(self) -> None:
        cookie = await self._login()
        stream = f"/api/v1/media/{self.media_id}/stream"
        first = await self.client.get(
            stream, headers={"Range": "bytes=131072-196607"}, cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(first.status, 206)
        self.assertEqual(await first.read(), self.buffer[131072:196608])
        for _ in range(20):
            if self.cache.has_chunk(str(self.media_id), 2):
                break
            await asyncio.sleep(0)
        self.assertEqual(self.reader.calls, [(0, 199999)])
        second = await self.client.get(
            stream, headers={"Range": "bytes=140000-150000"}, cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(await second.read(), self.buffer[140000:150001])
        # Chunk 2 is cached, so no further upstream read happens.
        self.assertEqual(self.reader.calls, [(0, 199999)])

    async def test_cache_stats_are_authenticated_and_report_stream_counters(self) -> None:
        denied = await self.client.get("/api/v1/cache-stats")
        self.assertEqual(denied.status, 401)

        cookie = await self._login()
        allowed = await self.client.get(
            "/api/v1/cache-stats", cookies={"tgvio_player_session": cookie}
        )
        self.assertEqual(allowed.status, 200)
        stats = await allowed.json()
        self.assertIn("bytes", stats)
        self.assertIn("disk_cache_bytes_served", stats)
        self.assertIn("inflight_bytes_served", stats)
        self.assertIn("upstream_bytes", stats)
        self.assertIn("prime_wait_ms_avg", stats)

    async def test_prepare_tail_warms_the_end_so_a_first_seek_is_local(self) -> None:
        cookie = await self._login()
        prepare = await self.client.post(
            f"/api/v1/media/{self.media_id}/prepare?tail=1",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(prepare.status, 202)
        self.assertEqual(await prepare.json(), {"prepared": True})

        # The covering window is warmed in the background, and never the head.
        for _ in range(100):
            if self.cache.has_chunk(self.media_id, 3):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.cache.has_chunk(self.media_id, 3))
        self.assertEqual(self.reader.calls, [(0, 199999)])

        # A viewer dragging to the end is then served without touching the archive.
        tail = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=199000-199999"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(tail.status, 206)
        self.assertEqual(await tail.read(), self.buffer[199000:200000])
        self.assertEqual(self.reader.calls, [(0, 199999)])

    async def test_prepare_without_tail_flag_does_not_warm_the_tail(self) -> None:
        cookie = await self._login()
        prepare = await self.client.post(
            f"/api/v1/media/{self.media_id}/prepare",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(prepare.status, 202)
        self.assertEqual(await prepare.json(), {"prepared": False})
        await asyncio.sleep(0.05)
        self.assertEqual(self.reader.calls, [])

    async def test_long_clip_prefetch_also_warms_the_tail(self) -> None:
        calls: list[tuple] = []
        original = self.cache.prefetch_tail
        self.cache.prefetch_tail = lambda *args, **kwargs: (calls.append(args), original(*args, **kwargs))[1]

        details = await self.repo.active_media_details(self.media_id)
        assert details is not None

        self.server._large_video_seconds = 10.0
        await self.server._schedule_prefetch(self.media_id, details)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], len(self.buffer))
        self.assertEqual(calls[0][4], min(64 * 1024, len(self.buffer)))

        calls.clear()
        self.server._large_video_seconds = 300.0
        await self.server._schedule_prefetch(self.media_id, details)
        self.assertEqual(calls, [], "a short clip must not pay for a tail warm")


if __name__ == "__main__":
    unittest.main()


class PlayerStreamCapacityFairnessTests(unittest.IsolatedAsyncioTestCase):
    """Regression cover for the 2026-09-30 production incident.

    One client held every global playback slot with its own streams, so its own
    current video was refused (429, ``reason=client_limit``) after a 3s wait.
    Browsers send ``Range: bytes=0-1`` to discover range support before real
    playback; those capability probes must never compete for playback capacity.
    """

    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.media_id = "a" * 64
        package = CatalogPackage(
            "package", "TGVIO/2026-09-22/1", "b" * 64, '"manifest"', '"complete"',
            (CatalogMedia(self.media_id, "video", 8, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        self.read_client = FakeReadClient()
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.read_client),
            deleter=FakeDeleteClient(),
            max_streams=4,
            max_streams_per_client=4,
            startup_cache=StartupRangeCache(max_entries=2, max_bytes=8),
            startup_range_bytes=1,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def _login(self) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.assertEqual(response.status, 200)
        return response.cookies["tgvio_player_session"].value

    async def _stream(self, cookie: str, range_header: str, preload: bool = False):
        headers = {"Range": range_header}
        if preload:
            headers["X-TGVIO-Preload"] = "1"
        return await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers=headers,
            cookies={"tgvio_player_session": cookie},
        )

    async def test_range_capability_probe_never_competes_with_playback(self) -> None:
        cookie = await self._login()
        held = [await self.server._acquire_stream(f"client-{index}") for index in range(4)]
        self.assertTrue(all(held))
        self.assertTrue(self.server.playback_saturated, "precondition: every playback slot is taken")
        try:
            response = await self._stream(cookie, "bytes=0-1")
            self.assertEqual(
                response.status, 206,
                "a 2-byte range-support probe must not be refused by playback capacity",
            )
            self.assertEqual(await response.read(), b"ab")
        finally:
            for index in range(4):
                await self.server._release_stream(f"client-{index}")
        self.assertFalse(self.server.playback_saturated)

    async def test_real_playback_still_waits_instead_of_being_mistaken_for_a_probe(self) -> None:
        cookie = await self._login()
        for index in range(4):
            await self.server._acquire_stream(f"client-{index}")
        try:
            response = await self._stream(cookie, "bytes=0-3")
            self.assertEqual(response.status, 429, "a real range still needs a playback slot")
        finally:
            for index in range(4):
                await self.server._release_stream(f"client-{index}")

    async def test_probe_budget_is_bounded_and_reports_its_own_reason(self) -> None:
        acquired = 0
        while await self.server._acquire_probe(diagnostics={}):
            acquired += 1
            if acquired > 64:
                self.fail("probe budget is unbounded")
        self.assertGreaterEqual(acquired, 1)
        diagnostics: dict[str, object] = {}
        self.assertFalse(await self.server._acquire_probe(diagnostics=diagnostics))
        self.assertEqual(diagnostics["reason"], "probe_limit")
        cookie = await self._login()
        response = await self._stream(cookie, "bytes=0-1")
        self.assertEqual(response.status, 429)
        for _ in range(acquired):
            await self.server._release_probe()

    async def test_one_client_cannot_reserve_the_whole_global_budget(self) -> None:
        self.assertLess(
            self.server._max_streams_per_client, self.server._max_streams,
            "a single client must not be able to consume every global slot",
        )
        for _ in range(self.server._max_streams_per_client):
            self.assertTrue(await self.server._acquire_stream("monopolist"))
        self.assertTrue(await self.server._acquire_stream("other-client"))
        await self.server._release_stream("other-client")
        for _ in range(self.server._max_streams_per_client):
            await self.server._release_stream("monopolist")

    async def test_short_upstream_fails_the_response_instead_of_stalling_the_client(self) -> None:
        """A short upstream must fail loudly, never leave a half-sent body.

        An explicit Content-Length disables aiohttp's own length check, so a
        handler that ends cleanly after fewer bytes keeps the connection open
        with a body that never arrives: the browser stalls with no error and
        never triggers its retry path.
        """
        cookie = await self._login()
        self.read_client.body = ClosableBody([])
        response = await self._stream(cookie, "bytes=0-3")
        with self.assertRaises(Exception) as caught:
            await asyncio.wait_for(response.read(), timeout=3)
        self.assertNotIsInstance(
            caught.exception, asyncio.TimeoutError,
            "a short upstream stalled the client instead of failing the response",
        )


class PlayerSimilarCoverTests(unittest.IsolatedAsyncioTestCase):
    """What looks like this cover: bounded, authenticated, and deterministic."""

    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.target, self.close, self.far, self.plain = "1" * 64, "2" * 64, "3" * 64, "4" * 64
        await self.repo.apply_package(CatalogPackage(
            "package", "TGVIO/2026-10-03/1", "b" * 64, '"manifest"', '"complete"',
            tuple(CatalogMedia(item, "video", 8, "video/mp4", 1080, 1920, 2.0)
                  for item in (self.target, self.close, self.far, self.plain)),
            tuple(CatalogLocation(item, "package", f"{item[0]}.mp4", '"etag"')
                  for item in (self.target, self.close, self.far, self.plain)),
            (
                CatalogCover(self.target, "package", "t.jpg", 10, "image/jpeg",
                             "bounded-frame-v2", "0000000000000000"),
                CatalogCover(self.close, "package", "c.jpg", 10, "image/jpeg",
                             "bounded-frame-v2", "0000000000000003"),
                CatalogCover(self.far, "package", "f.jpg", 10, "image/jpeg",
                             "bounded-frame-v2", "ffffffffffffffff"),
            ),
        ))
        await self.repo.refresh_media_activity()
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(FakeReadClient()),
            large_video_seconds=300,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def _login(self) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.assertEqual(response.status, 200)
        return response.cookies["tgvio_player_session"].value

    async def test_similar_is_authenticated_bounded_and_deterministic(self) -> None:
        self.assertEqual(
            (await self.client.get(f"/api/v1/media/{self.target}/similar")).status, 401
        )
        cookie = await self._login()
        cookies = {"tgvio_player_session": cookie}
        body = await (await self.client.get(
            f"/api/v1/media/{self.target}/similar", cookies=cookies
        )).json()
        self.assertEqual([item["id"] for item in body["items"]], [self.close],
                         "the far cover is outside the default band and the plain one has no hash")
        self.assertEqual(body["threshold"], 16)
        self.assertFalse(body["truncated"])
        tightened = await (await self.client.get(
            f"/api/v1/media/{self.target}/similar?threshold=1", cookies=cookies
        )).json()
        self.assertEqual(tightened["items"], [], "a threshold may only be tightened")
        for query in ("threshold=99", "threshold=abc", "limit=0", "limit=61"):
            self.assertEqual(
                (await self.client.get(
                    f"/api/v1/media/{self.target}/similar?{query}", cookies=cookies
                )).status, 400, query,
            )
        self.assertEqual(
            (await self.client.get(
                f"/api/v1/media/{'9' * 64}/similar", cookies=cookies
            )).status, 404,
        )

    async def test_a_cover_without_a_fingerprint_has_no_similar_list(self) -> None:
        cookie = await self._login()
        body = await (await self.client.get(
            f"/api/v1/media/{self.plain}/similar",
            cookies={"tgvio_player_session": cookie},
        )).json()
        self.assertEqual(body["items"], [], "no similarity information is not an error")
        self.assertFalse(body["truncated"])

    async def test_a_retired_cover_row_never_reaches_the_similar_list(self) -> None:
        cookie = await self._login()
        cookies = {"tgvio_player_session": cookie}
        before = await (await self.client.get(
            f"/api/v1/media/{self.target}/similar", cookies=cookies)).json()
        self.assertEqual([item["id"] for item in before["items"]], [self.close])
        # The row that carried the close fingerprint is retired. Its hash is still in the
        # table, but it is no longer an active cover, so it may not be a neighbour either.
        await self.repo.record_deleted_cover(self.close, "package")
        after = await (await self.client.get(
            f"/api/v1/media/{self.target}/similar", cookies=cookies)).json()
        self.assertEqual(
            [item["id"] for item in after["items"]], [],
            "a retired cover row must not answer a similarity query",
        )

    async def test_a_bounded_scan_says_when_it_stopped_early(self) -> None:
        cookie = await self._login()
        with patch("tgvio_player.adapters.http.media._MAX_SIMILAR_SCAN", 1):
            body = await (await self.client.get(
                f"/api/v1/media/{self.target}/similar",
                cookies={"tgvio_player_session": cookie},
            )).json()
        self.assertTrue(body["truncated"], "the scan hit its ceiling and says so")

class PlayerCoverMirrorTests(unittest.IsolatedAsyncioTestCase):
    """The cover route answers from the local mirror before it asks the archive.

    The mirror is a cache in front of the cover budget, not a second source of truth: the
    route still resolves the active cover row and its version first, and a mirror hit only
    saves the round trip.
    """

    JPEG = b"\xff\xd8\xff\xe0cover-bytes"
    DIGEST = "d" * 64
    ADDRESSED = f"cover/backfill/{DIGEST}.jpg"
    PLAIN = "cover/video-cover.jpg"

    class FakeCoverReader:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.calls: list[tuple[str, ByteRange | None]] = []
            self.fail: Exception | None = None

        async def open_range(self, remote_path: str, byte_range: ByteRange | None) -> WebDavRangeResponse:
            self.calls.append((remote_path, byte_range))
            if self.fail is not None:
                raise self.fail
            length = len(self.payload) if byte_range is None else byte_range.length
            return WebDavRangeResponse(
                206, "image/jpeg", length, None, '"cover"', ClosableBody([self.payload])
            )

    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.media_id = "c" * 64
        self.counter = 0
        self.mirror_root = Path(self.tmp.name) / "covers"
        self.client = None
        await self._publish(self.ADDRESSED)

    async def asyncTearDown(self) -> None:
        if self.client is not None:
            await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def _publish(self, relpath: str) -> None:
        """One package per call: a fresh package_id keeps the newest cover the active one."""
        self.counter += 1
        package_id = f"package-{self.counter}"
        await self.repo.apply_package(CatalogPackage(
            package_id, f"TGVIO/2026-09-22/{self.counter}", "b" * 64, f'"{package_id}"', '"complete"',
            (CatalogMedia(self.media_id, "video", 8, "video/mp4", 1080, 1920, 2.0),),
            (CatalogLocation(self.media_id, package_id, "video.mp4", '"etag"'),),
            (
                CatalogCover(
                    media_id=self.media_id, package_id=package_id, remote_relpath=relpath,
                    size_bytes=len(self.JPEG), mime_type="image/jpeg",
                    algorithm="reuse-publish-thumbnail-v1",
                ),
            ),
        ))
        await self.repo.refresh_media_activity()

    async def _serve(self, mirror: CoverMirror | None) -> tuple[CoverMirrorCounters, FakeCoverReader]:
        self.reader = self.FakeCoverReader(self.JPEG)
        self.counters = CoverMirrorCounters()
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            ReadOnlyWebDavAdapter(self.reader),
            max_streams=4,
            max_streams_per_client=4,
            cover_mirror=mirror,
            cover_mirror_counters=self.counters,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()
        return self.counters, self.reader

    async def _cover(self):
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        cookie = response.cookies["tgvio_player_session"].value
        return await self.client.get(
            f"/api/v1/media/{self.media_id}/cover", cookies={"tgvio_player_session": cookie}
        )

    async def test_a_mirrored_cover_is_served_without_touching_the_archive(self) -> None:
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        mirror.write(self.DIGEST, self.JPEG)
        counters, reader = await self._serve(mirror)
        response = await self._cover()
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.JPEG)
        self.assertEqual(reader.calls, [], "a mirror hit costs no upstream round trip")
        self.assertEqual((counters.hits, counters.misses), (1, 0))

    async def _await_mirror(self, mirror: CoverMirror, key: str, size: int) -> bool:
        """The fill is best effort and rides alongside the response, so it may land after it."""
        for _ in range(200):
            if mirror.has(key, size):
                return True
            await asyncio.sleep(0.01)
        return False

    async def test_a_miss_reads_upstream_and_fills_the_mirror(self) -> None:
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        counters, reader = await self._serve(mirror)
        first = await self._cover()
        self.assertEqual(await first.read(), self.JPEG)
        self.assertEqual(len(reader.calls), 1)
        self.assertTrue(
            await self._await_mirror(mirror, self.DIGEST, len(self.JPEG)),
            "the first read fills the mirror",
        )
        second = await self._cover()
        self.assertEqual(await second.read(), self.JPEG)
        self.assertEqual(len(reader.calls), 1, "the second request never left the host")
        self.assertEqual((counters.hits, counters.misses), (1, 1))

    async def test_the_mirror_does_not_change_the_response_contract(self) -> None:
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        await self._serve(mirror)
        upstream = await self._cover()
        upstream_headers = {name: upstream.headers[name] for name in
                            ("Content-Type", "Content-Length", "Content-Disposition")}
        self.assertEqual(await upstream.read(), self.JPEG)
        mirror.write(self.DIGEST, self.JPEG)
        served = await self._cover()
        self.assertEqual(
            {name: served.headers[name] for name in upstream_headers}, upstream_headers,
            "a mirrored cover answers with the same three headers",
        )
        self.assertEqual(await served.read(), self.JPEG)

    async def test_an_upstream_failure_still_serves_a_mirrored_cover(self) -> None:
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        mirror.write(self.DIGEST, self.JPEG + b"-a-different-size")
        counters, reader = await self._serve(mirror)
        reader.fail = OSError("archive down")
        response = await self._cover()
        self.assertEqual(response.status, 200, "the local copy answers when the archive cannot")
        self.assertEqual(await response.read(), self.JPEG + b"-a-different-size")
        self.assertEqual(counters.hits, 0, "an unverified size is a fallback, not a hit")

    async def test_a_mirrored_cover_does_not_need_the_cover_budget(self) -> None:
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        mirror.write(self.DIGEST, self.JPEG)
        await self._serve(mirror)
        self.server._max_cover = 0                     # every upstream cover would be shed
        response = await self._cover()
        self.assertEqual(response.status, 200, "the local copy never waits for a cover slot")
        self.assertEqual(await response.read(), self.JPEG)

    async def test_an_unreadable_key_keeps_todays_behaviour(self) -> None:
        await self._publish(self.PLAIN)
        # The first package's cover holds the active slot (active_cover orders by package
        # id), so retire it: the plain cover is then the one this media serves.
        await self.repo.record_deleted_cover(self.media_id, "package-1")
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        counters, reader = await self._serve(mirror)
        self.assertIsNone(mirror.key_for(self.PLAIN))
        response = await self._cover()
        self.assertEqual(response.status, 200)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(mirror.stats(), (0, 0), "a cover that is not content-addressed is never mirrored")
        self.assertEqual((counters.hits, counters.misses), (0, 1))

    async def test_the_mirror_off_is_exactly_today(self) -> None:
        counters, reader = await self._serve(None)
        response = await self._cover()
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.JPEG)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual((counters.hits, counters.misses), (0, 0))
        self.assertFalse(self.mirror_root.exists(), "off writes nothing at all")

    async def test_health_reports_what_the_mirror_did(self) -> None:
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        counters, reader = await self._serve(mirror)
        await self._cover()                       # one miss, which fills the mirror
        self.assertTrue(await self._await_mirror(mirror, self.DIGEST, len(self.JPEG)))
        body = await (await self.client.get("/healthz")).json()
        report = body["stream_capacity"]["cover_mirror"]
        self.assertTrue(report["enabled"])
        self.assertEqual(report["files"], 1)
        self.assertEqual(report["bytes"], len(self.JPEG))
        self.assertEqual((report["hits"], report["misses"]), (0, 1))
        self.assertEqual((report["warm_pending"], report["warm_failed"]), (0, 0))

    async def test_health_says_when_the_mirror_is_off(self) -> None:
        await self._serve(None)
        body = await (await self.client.get("/healthz")).json()
        report = body["stream_capacity"]["cover_mirror"]
        self.assertFalse(report["enabled"])
        self.assertEqual((report["files"], report["bytes"], report["hits"], report["misses"]), (0, 0, 0, 0))
