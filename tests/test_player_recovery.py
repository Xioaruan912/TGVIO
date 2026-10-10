from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.application.player_recovery import (
    CONFIG_CONTEXT,
    FAVORITES_CONTEXT,
    EncryptedManifestStore,
    PlayerRecoveryService,
    RecoveryError,
    WebDavBootstrap,
)
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.storage_settings import PlayerStorageSettings
from tgvio_player.infrastructure.player_crypto import PlayerStateCipher
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite


class MemoryDav:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = set()
        self.fail_move = False
        self.fail_put = False
        self.fail_put_path: str | None = None

    async def ensure_directory(self, path: str) -> None:
        self.directories.add(path)

    async def put_stream(self, path: str, chunks, *, size_bytes: int, content_type: str):
        if self.fail_put or path == self.fail_put_path:
            raise OSError("simulated write failure")
        payload = bytearray()
        async for chunk in chunks:
            payload.extend(chunk)
        if len(payload) != size_bytes:
            raise ValueError("size mismatch")
        self.files[path] = bytes(payload)
        return type("Receipt", (), {"status_code": 201, "size_bytes": len(payload), "etag": None})()

    async def get_bytes(self, path: str, *, max_bytes: int) -> bytes | None:
        value = self.files.get(path)
        if value is not None and len(value) > max_bytes:
            raise ValueError("size limit")
        return value

    async def open_stream(self, path: str):
        value = self.files[path]

        async def chunks():
            for offset in range(0, len(value), 3):
                yield value[offset:offset + 3]

        return len(value), "video/mp4", chunks()

    async def stat(self, path: str):
        value = self.files.get(path)
        return None if value is None else type("Stat", (), {"size_bytes": len(value), "etag": None})()

    async def delete(self, path: str):
        self.files.pop(path, None)
        return type("Receipt", (), {"status_code": 204, "deleted": True})()

    async def move(self, source: str, target: str, *, overwrite: bool) -> None:
        if self.fail_move:
            raise OSError("simulated pointer replacement failure")
        if source not in self.files:
            raise FileNotFoundError(source)
        if target in self.files and not overwrite:
            raise FileExistsError(target)
        self.files[target] = self.files.pop(source)


class MemoryDavFactory:
    def __init__(self) -> None:
        self.clients: dict[str, MemoryDav] = {}

    def __call__(self, endpoint_url: str, username: str, password: str) -> MemoryDav:
        return self.clients.setdefault(endpoint_url, MemoryDav())


class EncryptedManifestStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_pointer_update_does_not_require_webdav_move(self) -> None:
        dav = MemoryDav()
        cipher = PlayerStateCipher(base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="))
        store = EncryptedManifestStore(dav, cipher, "root", context=FAVORITES_CONTEXT)
        await store.save_atomic({"schema_version": 1, "revision": 1, "items": []})
        dav.fail_move = True
        await store.save_atomic({"schema_version": 1, "revision": 2, "items": []})
        loaded = await store.load()
        self.assertEqual(loaded["revision"], 2)

    async def test_only_the_last_three_revisions_stay_on_the_storage(self) -> None:
        dav = MemoryDav()
        cipher = PlayerStateCipher(base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="))
        store = EncryptedManifestStore(dav, cipher, "root", context=FAVORITES_CONTEXT)
        for revision in range(1, 7):
            await store.save_atomic({"schema_version": 1, "revision": revision, "items": []})
        revisions = sorted(name for name in dav.files if name.count(".") == 2)
        self.assertEqual([name.split(".")[1] for name in revisions], ["4", "5", "6"])
        self.assertEqual((await store.load())["revision"], 6)

    async def test_failed_pointer_put_preserves_last_valid_manifest(self) -> None:
        dav = MemoryDav()
        cipher = PlayerStateCipher(base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("="))
        store = EncryptedManifestStore(dav, cipher, "root", context=FAVORITES_CONTEXT)
        await store.save_atomic({"schema_version": 1, "revision": 1, "items": []})
        dav.fail_put_path = "root/favorites-manifest.enc"
        with self.assertRaises(OSError):
            await store.save_atomic({"schema_version": 1, "revision": 2, "items": []})
        dav.fail_put_path = None
        loaded = await store.load()
        self.assertEqual(loaded["revision"], 1)

    async def test_unreferenced_partial_revision_can_be_reused(self) -> None:
        dav = MemoryDav()
        cipher = PlayerStateCipher(base64.urlsafe_b64encode(b"o" * 32).decode().rstrip("="))
        store = EncryptedManifestStore(dav, cipher, "root", context=FAVORITES_CONTEXT)
        abandoned = {"schema_version": 1, "revision": 2, "items": [{"old": True}]}
        dav.files["root/favorites-manifest.2.enc"] = cipher.encrypt(
            json.dumps(abandoned).encode(), context=FAVORITES_CONTEXT,
        )

        expected = {"schema_version": 1, "revision": 2, "items": []}
        await store.save_atomic(expected)

        self.assertEqual(await store.load(), expected)

    async def test_truncated_or_modified_revision_fails_authentication(self) -> None:
        dav = MemoryDav()
        cipher = PlayerStateCipher(base64.urlsafe_b64encode(b"m" * 32).decode().rstrip("="))
        store = EncryptedManifestStore(dav, cipher, "root", context=FAVORITES_CONTEXT)
        await store.save_atomic({"schema_version": 1, "revision": 1, "items": []})
        revision_path = "root/favorites-manifest.1.enc"
        dav.files[revision_path] = dav.files[revision_path][:-4]
        with self.assertRaises(RecoveryError):
            await store.load()


class PlayerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.repo = PlayerCatalogRepositorySQLite(self.path / "old.sqlite3")
        await self.repo.open()
        self.media_id = "a" * 64
        self.pending_id = "c" * 64
        self.deleting_id = "d" * 64
        await self.repo.apply_package(CatalogPackage(
            "package", "archive/package", "b" * 64, None, None,
            (CatalogMedia(self.media_id, "video", 6, "video/mp4"),
             CatalogMedia(self.pending_id, "video", 7, "video/mp4"),
             CatalogMedia(self.deleting_id, "video", 8, "video/mp4")),
            (CatalogLocation(self.media_id, "package", "clip.mp4"),
             CatalogLocation(self.pending_id, "package", "pending.mp4"),
             CatalogLocation(self.deleting_id, "package", "deleted.mp4")),
        ))
        await self.repo.refresh_media_activity()
        await self.repo.set_global_favorite(self.media_id, True)
        self.key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
        self.cipher = PlayerStateCipher(self.key)
        self.settings = PlayerStorageSettings(
            "https://dav-old.example.test", "player", "Favorites",
            self.cipher.encrypt(b"alice", context=b"webdav-username"),
            self.cipher.encrypt(b"old-password", context=b"webdav-password"), 1,
        )
        await self.repo.save_storage_settings(self.settings)
        await self.repo.save_favorite_location(self.media_id, "Favorites/clip.mp4", 6, "video/mp4")
        await self.repo.set_global_favorite(self.deleting_id, True)
        await self.repo.save_favorite_location(self.deleting_id, "Favorites/deleted.mp4", 8, "video/mp4")
        await self.repo.set_global_favorite(self.deleting_id, False)
        await self.repo.enqueue_favorite_sync(self.deleting_id, "delete")
        self.factory = MemoryDavFactory()
        old_dav = self.factory(self.settings.endpoint_url, "alice", "old-password")
        old_dav.files["player/Favorites/clip.mp4"] = b"video!"
        old_dav.files["player/Favorites/deleted.mp4"] = b"delete!!"
        self.service = PlayerRecoveryService(self.repo, self.cipher, self.factory)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_export_is_encrypted_and_clean_vps_restores_favorites(self) -> None:
        await self.repo.set_global_favorite(self.pending_id, True)
        self.repo._require().execute(
            "DELETE FROM favorite_locations WHERE media_id=?", (self.pending_id,)
        )
        self.repo._require().commit()
        await self.service.export_state()
        remote = self.factory.clients[self.settings.endpoint_url]
        self.assertNotIn(b"old-password", b"".join(remote.files.values()))
        self.assertNotIn(self.key.encode(), b"".join(remote.files.values()))

        new_repo = PlayerCatalogRepositorySQLite(self.path / "new.sqlite3")
        await new_repo.open()
        try:
            restored = PlayerRecoveryService(new_repo, self.cipher, self.factory)
            result = await restored.restore(WebDavBootstrap(
                self.settings.endpoint_url, "player", "alice", "old-password"
            ))
            self.assertTrue(result.restored)
            self.assertEqual(await new_repo.get_storage_settings(), PlayerStorageSettings(
                self.settings.endpoint_url, self.settings.player_root, self.settings.favorites_dir,
                self.settings.username_ciphertext, self.settings.password_ciphertext, 2,
            ))
            self.assertEqual(
                {str(row[0]) for row in new_repo._require().execute(
                    "SELECT media_id FROM player_global_favorites"
                ).fetchall()},
                {self.media_id, self.pending_id},
            )
            self.assertEqual(
                await new_repo.list_favorite_locations(),
                [(self.media_id, "Favorites/clip.mp4", 6, "video/mp4"),
                 (self.deleting_id, "Favorites/deleted.mp4", 8, "video/mp4")],
            )
            jobs = await new_repo.claim_favorite_sync(limit=10)
            self.assertEqual(
                [(job.media_id, job.operation) for job in jobs],
                [(self.pending_id, "upload"), (self.deleting_id, "delete")],
            )
        finally:
            await new_repo.close()

    async def test_wrong_recovery_key_and_future_schema_fail_closed(self) -> None:
        await self.service.export_state()
        new_repo = PlayerCatalogRepositorySQLite(self.path / "wrong-key.sqlite3")
        await new_repo.open()
        try:
            wrong = PlayerStateCipher(base64.urlsafe_b64encode(b"z" * 32).decode().rstrip("="))
            with self.assertRaises(RecoveryError):
                await PlayerRecoveryService(new_repo, wrong, self.factory).restore(
                    WebDavBootstrap(self.settings.endpoint_url, "player", "alice", "old-password")
                )
        finally:
            await new_repo.close()

        remote = self.factory.clients[self.settings.endpoint_url]
        config = {
            "schema_version": 999, "revision": 9,
            "endpoint_url": self.settings.endpoint_url, "player_root": "player",
            "favorites_dir": "Favorites", "username_ciphertext": None,
            "password_ciphertext": None,
        }
        remote.files["player/player-config.9.enc"] = self.cipher.encrypt(
            json.dumps(config).encode(), context=CONFIG_CONTEXT
        )
        remote.files["player/player-config.enc"] = json.dumps({
            "schema_version": 1, "revision": 9, "file": "player-config.9.enc",
            "sha256": hashlib.sha256(remote.files["player/player-config.9.enc"]).hexdigest(),
        }).encode()
        newer_repo = PlayerCatalogRepositorySQLite(self.path / "future.sqlite3")
        await newer_repo.open()
        try:
            with self.assertRaises(RecoveryError):
                await PlayerRecoveryService(newer_repo, self.cipher, self.factory).restore(
                    WebDavBootstrap(self.settings.endpoint_url, "player", "alice", "old-password")
                )
        finally:
            await newer_repo.close()

    async def test_newer_local_revision_is_not_overwritten(self) -> None:
        await self.service.export_state()
        local_settings = PlayerStorageSettings(
            "https://local.example.test", "local-root", "Local", None, None, 100,
        )
        await self.repo.save_storage_settings(local_settings)
        with self.assertRaises(RecoveryError):
            await self.service.restore(WebDavBootstrap(
                self.settings.endpoint_url, "player", "alice", "old-password"
            ))
        self.assertEqual(await self.repo.get_storage_settings(), local_settings)

    async def test_target_migration_verifies_copies_before_activation(self) -> None:
        old = self.factory.clients[self.settings.endpoint_url]
        old.files["player/Favorites/clip.mp4"] = b"video!"
        new_settings = PlayerStorageSettings(
            "https://dav-new.example.test", "new-root", "Saved",
            self.cipher.encrypt(b"bob", context=b"webdav-username"),
            self.cipher.encrypt(b"new-password", context=b"webdav-password"), 2,
        )
        outcome = await self.service.migrate_target(new_settings)
        self.assertTrue(outcome.migrated)
        self.assertEqual(await self.repo.get_storage_settings(), new_settings)
        copied = self.factory.clients[new_settings.endpoint_url]
        self.assertEqual(copied.files["new-root/Saved/clip.mp4"], b"video!")

        partial = PlayerStorageSettings(
            "https://dav-partial.example.test", "partial-root", "Saved",
            self.cipher.encrypt(b"carol", context=b"webdav-username"),
            self.cipher.encrypt(b"partial-password", context=b"webdav-password"), 3,
        )
        partial_dav = self.factory(partial.endpoint_url, "carol", "partial-password")
        partial_dav.fail_put_path = "partial-root/player-config.enc"
        with self.assertRaises(RecoveryError):
            await self.service.migrate_target(partial)
        self.assertEqual(await self.repo.get_storage_settings(), new_settings)
        partial_dav.fail_put_path = None
        retried = await self.service.migrate_target(partial)
        self.assertTrue(retried.migrated)
        self.assertEqual(retried.revision, 3)

        failing = PlayerStorageSettings(
            "https://dav-failing.example.test", "failed-root", "Saved", None, None, 3,
        )
        self.factory(failing.endpoint_url, "", "").fail_put = True
        with self.assertRaises(RecoveryError):
            await self.service.migrate_target(failing)
        self.assertEqual(await self.repo.get_storage_settings(), PlayerStorageSettings(
            partial.endpoint_url, partial.player_root, partial.favorites_dir,
            partial.username_ciphertext, partial.password_ciphertext, retried.revision,
        ))
    async def test_collections_travel_with_the_backup_and_come_back(self) -> None:
        manual = await self.repo.create("旅行", "manual", None)
        await self.repo.add_item(manual.collection_id, self.media_id)
        await self.repo.add_item(manual.collection_id, self.deleting_id)
        await self.repo.create("短片", "smart", '{"min_seconds": 1}')
        await self.service.export_state()

        new_repo = PlayerCatalogRepositorySQLite(self.path / "collections.sqlite3")
        await new_repo.open()
        try:
            result = await PlayerRecoveryService(new_repo, self.cipher, self.factory).restore(
                WebDavBootstrap(self.settings.endpoint_url, "player", "alice", "old-password")
            )
            self.assertTrue(result.restored)
            restored = {item.name: item for item in await new_repo.list()}
            self.assertEqual(sorted(restored), ["旅行", "短片"])
            self.assertEqual(
                (restored["旅行"].kind, restored["短片"].kind), ("manual", "smart")
            )
            self.assertEqual(restored["短片"].rules_json, '{"min_seconds": 1}')
            self.assertEqual(
                await new_repo.items(restored["旅行"].collection_id, 50, 0),
                (self.media_id, self.deleting_id),
            )
        finally:
            await new_repo.close()

    async def test_a_manifest_written_by_the_previous_build_restores_without_collections(self) -> None:
        """A v1 payload is valid input: it simply carries no collections."""
        await self.service.export_state()
        remote = self.factory.clients[self.settings.endpoint_url]
        config_store = EncryptedManifestStore(
            remote, self.cipher, "player", context=CONFIG_CONTEXT, name="player-config",
        )
        config = await config_store.load()
        config["revision"] = int(config["revision"]) + 1
        await config_store.save_atomic(config)
        previous = await EncryptedManifestStore(remote, self.cipher, "player").load()
        previous.pop("collections")
        previous["schema_version"] = 1
        previous["revision"] = config["revision"]
        await EncryptedManifestStore(
            remote, self.cipher, "player", schema_version=1
        ).save_atomic(previous)

        new_repo = PlayerCatalogRepositorySQLite(self.path / "v1.sqlite3")
        await new_repo.open()
        try:
            result = await PlayerRecoveryService(new_repo, self.cipher, self.factory).restore(
                WebDavBootstrap(self.settings.endpoint_url, "player", "alice", "old-password")
            )
            self.assertTrue(result.restored)
            self.assertEqual(await new_repo.list(), ())
            self.assertEqual(
                {str(row[0]) for row in new_repo._require().execute(
                    "SELECT media_id FROM player_global_favorites"
                ).fetchall()},
                {self.media_id},
            )
        finally:
            await new_repo.close()

    async def test_a_member_the_server_has_not_catalogued_waits_as_a_placeholder(self) -> None:
        """A fresh install restores before its catalog finishes syncing."""
        manual = await self.repo.create("旅行", "manual", None)
        await self.repo.add_item(manual.collection_id, self.pending_id)
        await self.service.export_state()

        new_repo = PlayerCatalogRepositorySQLite(self.path / "placeholder.sqlite3")
        await new_repo.open()
        try:
            await PlayerRecoveryService(new_repo, self.cipher, self.factory).restore(
                WebDavBootstrap(self.settings.endpoint_url, "player", "alice", "old-password")
            )
            restored = (await new_repo.list())[0]
            self.assertEqual(await new_repo.items(restored.collection_id, 50, 0), ())
            self.assertEqual(
                new_repo._require().execute(
                    "SELECT COUNT(*) FROM collection_items WHERE collection_id=?",
                    (restored.collection_id,),
                ).fetchone()[0],
                1,
                "the membership is real even while the video is not",
            )
            await new_repo.apply_package(CatalogPackage(
                "package", "archive/package", "b" * 64, None, None,
                (CatalogMedia(self.pending_id, "video", 7, "video/mp4"),),
                (CatalogLocation(self.pending_id, "package", "pending.mp4"),),
            ))
            await new_repo.refresh_media_activity()
            self.assertEqual(
                await new_repo.items(restored.collection_id, 50, 0), (self.pending_id,)
            )
        finally:
            await new_repo.close()

    async def test_restoring_twice_neither_duplicates_collections_nor_loses_members(self) -> None:
        manual = await self.repo.create("旅行", "manual", None)
        await self.repo.add_item(manual.collection_id, self.media_id)
        await self.service.export_state()

        new_repo = PlayerCatalogRepositorySQLite(self.path / "twice.sqlite3")
        await new_repo.open()
        try:
            service = PlayerRecoveryService(new_repo, self.cipher, self.factory)
            bootstrap = WebDavBootstrap(
                self.settings.endpoint_url, "player", "alice", "old-password"
            )
            await service.restore(bootstrap)
            await service.restore(bootstrap)
            restored = await new_repo.list()
            self.assertEqual([item.name for item in restored], ["旅行"])
            self.assertEqual(
                await new_repo.items(restored[0].collection_id, 50, 0), (self.media_id,)
            )
        finally:
            await new_repo.close()

    async def test_target_migration_writes_the_collections_into_the_new_manifest(self) -> None:
        manual = await self.repo.create("旅行", "manual", None)
        await self.repo.add_item(manual.collection_id, self.media_id)
        new_settings = PlayerStorageSettings(
            "https://dav-collections.example.test", "moved-root", "Saved",
            self.cipher.encrypt(b"bob", context=b"webdav-username"),
            self.cipher.encrypt(b"new-password", context=b"webdav-password"), 2,
        )
        await self.service.migrate_target(new_settings)
        copied = self.factory.clients[new_settings.endpoint_url]
        manifest = await EncryptedManifestStore(copied, self.cipher, "moved-root").load()
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(
            [
                (entry["name"], entry["kind"], entry["items"])
                for entry in manifest["collections"]
            ],
            [("旅行", "manual", [self.media_id])],
        )


if __name__ == "__main__":
    unittest.main()
