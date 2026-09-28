from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.application.favorite_backup import (
    FavoriteBackupService,
    SourceMediaError,
)
from tgvio_player.application.ports import DeleteReceipt, RemoteFileStat, UploadReceipt
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.storage_settings import PlayerStorageSettings
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_write import WebDavWriteError


class FakeWriter:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.put_error: WebDavWriteError | None = None
        self.copy_payload: bytes | None = None
        self.delete_error: WebDavWriteError | None = None
        self.bad_stat = False
        self.max_chunk = 0
        self.events: list[str] = []

    async def ensure_directory(self, path: str) -> None:
        return None

    async def put_stream(self, path: str, chunks, *, size_bytes: int, content_type: str):
        self.calls.append(path)
        if self.put_error is not None:
            raise self.put_error
        payload = bytearray()
        async for chunk in chunks:
            self.max_chunk = max(self.max_chunk, len(chunk))
            payload.extend(chunk)
        self.files[path] = bytes(payload)
        return UploadReceipt(201, len(payload), '"file"')

    async def stat(self, path: str):
        value = self.files.get(path)
        if value is None:
            return None
        return RemoteFileStat(len(value) + (1 if self.bad_stat else 0), '"file"')

    async def copy(self, source: str, target: str) -> None:
        self.calls.append(f"copy:{source}->{target}")
        if self.copy_payload is None:
            raise WebDavWriteError("copy", "copy_unsupported", 404)
        self.files[target] = self.copy_payload

    async def delete(self, path: str):
        self.calls.append(path)
        self.events.append("delete")
        if self.delete_error is not None:
            raise self.delete_error
        self.files.pop(path, None)
        return DeleteReceipt(204, True)


class SnapshotSpy:
    def __init__(self, repo: PlayerCatalogRepositorySQLite, events: list[str]) -> None:
        self.repo = repo
        self.events = events
        self.fail = False

    async def export_state(self):
        if self.fail:
            raise RuntimeError("snapshot unavailable")
        jobs = await self.repo.list_pending_favorite_sync()
        if any(job.operation == "delete" for job in jobs):
            self.events.append("tombstone")
        else:
            self.events.append("snapshot")
        return None


class FavoriteBackupServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.media_id = "a" * 64
        self.payload = b"video payload"
        await self.repo.apply_package(CatalogPackage(
            "archive", "archive/2026/1", "b" * 64, None, None,
            (CatalogMedia(self.media_id, "video", len(self.payload), "video/mp4", container="mp4"),),
            (CatalogLocation(self.media_id, "archive", "source.mp4"),),
        ))
        await self.repo.refresh_media_activity()
        await self.repo.save_storage_settings(PlayerStorageSettings(
            "https://dav.example.test", "player", "Favorites", revision=1,
        ))
        self.writer = FakeWriter()
        self.events: list[str] = []
        self.writer.events = self.events
        self.snapshot_exporter = SnapshotSpy(self.repo, self.events)
        self.source_calls = 0

        async def source(_media_id: str):
            self.source_calls += 1

            async def chunks() -> AsyncIterator[bytes]:
                for offset in range(0, len(self.payload), 4):
                    yield self.payload[offset:offset + 4]

            return len(self.payload), "video/mp4", chunks()

        self.source = source
        self.service = FavoriteBackupService(
            self.repo, self.writer, self.source, self.snapshot_exporter,
            clock=lambda: 1000,
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_favorite_enqueues_one_upload_and_duplicate_is_idempotent(self) -> None:
        first = await self.service.favorite(self.media_id)
        second = await self.service.favorite(self.media_id)
        self.assertEqual(first.sync_status, "pending")
        self.assertEqual(second.sync_status, "pending")
        jobs = await self.repo.list_pending_favorite_sync()
        self.assertEqual([(job.media_id, job.operation) for job in jobs], [(self.media_id, "upload")])
        self.assertEqual(jobs[0].status, "pending")
        self.assertEqual(self.writer.calls, [])

    async def test_upload_is_streamed_and_only_marked_synced_after_stat(self) -> None:
        await self.service.favorite(self.media_id)
        result = await self.service.sync_pending()
        self.assertEqual((result.synced, result.failed, result.retried), (1, 0, 0))
        expected_path = f"player/Favorites/{self.media_id}.mp4"
        self.assertEqual(self.writer.files[expected_path], self.payload)
        self.assertEqual(self.writer.max_chunk, 4)
        self.assertEqual(
            await self.repo.list_favorite_locations(),
            [(self.media_id, f"Favorites/{self.media_id}.mp4", len(self.payload), "video/mp4")],
        )
        self.assertEqual(self.events, ["snapshot"])
        calls_before_duplicate = list(self.writer.calls)
        repeated = await self.service.favorite(self.media_id)
        self.assertEqual(repeated.sync_status, "synced")
        self.assertEqual(self.writer.calls, calls_before_duplicate)
        self.assertEqual(await self.repo.list_pending_favorite_sync(), [])

    async def test_same_openlist_source_is_copied_server_side_without_streaming(self) -> None:
        self.writer.copy_payload = self.payload

        async def source_location(_media_id: str):
            return "archive/2026/1", "nested/source.mp4"

        service = FavoriteBackupService(
            self.repo, self.writer, self.source, self.snapshot_exporter,
            source_location=source_location, clock=lambda: 1000,
        )
        await service.favorite(self.media_id)
        result = await service.sync_pending()

        expected = f"Favorites/{self.media_id}/source.mp4"
        self.assertEqual((result.synced, result.failed, result.retried), (1, 0, 0))
        self.assertEqual(self.source_calls, 0)
        self.assertIn(
            f"copy:archive/2026/1/nested/source.mp4->player/{expected}", self.writer.calls,
        )
        self.assertEqual(
            await self.repo.list_favorite_locations(),
            [(self.media_id, expected, len(self.payload), "video/mp4")],
        )

    async def test_size_or_stat_mismatch_does_not_mark_copy_synced(self) -> None:
        self.writer.bad_stat = True
        await self.service.favorite(self.media_id)
        result = await self.service.sync_pending()
        self.assertEqual((result.synced, result.retried, result.failed), (0, 1, 0))
        self.assertEqual(await self.repo.list_favorite_locations(), [])
        job = await self.repo.list_pending_favorite_sync()
        self.assertEqual(job[0].status, "retry")
        self.assertEqual(job[0].error_code, "verification_mismatch")

    async def test_webdav_failure_log_keeps_media_operation_category_and_status(self) -> None:
        self.writer.put_error = WebDavWriteError("put", "rejected", 413)
        await self.service.favorite(self.media_id)

        with self.assertLogs("tgvio_player.favorite_backup", level="WARNING") as captured:
            result = await self.service.sync_pending()

        self.assertEqual((result.synced, result.failed, result.retried), (0, 1, 0))
        message = "\n".join(captured.output)
        self.assertIn(f"media_id={self.media_id}", message)
        self.assertIn("operation=put", message)
        self.assertIn("category=rejected", message)
        self.assertIn("status=413", message)

    async def test_missing_original_source_is_classified_without_claiming_success(self) -> None:
        async def missing(_media_id: str):
            raise SourceMediaError("source_not_found", 404)

        self.service = FavoriteBackupService(
            self.repo, self.writer, missing, SnapshotSpy(self.repo, self.events), clock=lambda: 1000,
        )
        await self.service.favorite(self.media_id)
        result = await self.service.sync_pending()
        self.assertEqual((result.synced, result.failed), (0, 1))
        self.assertEqual(self.writer.calls, [])
        job = await self.repo.list_pending_favorite_sync()
        self.assertEqual(job, [])
        row = self.repo._require().execute(
            "SELECT status, error_code FROM favorite_sync WHERE media_id=?", (self.media_id,)
        ).fetchone()
        self.assertEqual((row["status"], row["error_code"]), ("failed", "source_404"))

    async def test_unfavorite_persists_tombstone_before_delete_and_never_reads_archive(self) -> None:
        await self.service.favorite(self.media_id)
        await self.service.sync_pending()
        self.events.clear()
        source_calls = self.source_calls
        await self.service.unfavorite(self.media_id)
        self.assertEqual(self.events, ["tombstone"])
        self.writer.delete_error = WebDavWriteError("delete", "server_error", 503)
        result = await self.service.sync_pending()
        self.assertEqual((result.retried, result.synced), (1, 0))
        self.assertEqual(self.events, ["tombstone", "delete"])
        self.assertEqual(self.source_calls, source_calls)
        self.assertEqual(self.writer.calls[-1], f"player/Favorites/{self.media_id}.mp4")
        self.assertFalse(await self.repo.is_global_favorite(self.media_id))
        job = await self.repo.list_pending_favorite_sync()
        self.assertEqual([(item.operation, item.status, item.error_code) for item in job],
                         [("delete", "retry", "server_error")])

    async def test_confirmed_delete_removes_copy_location_and_outbox_tombstone(self) -> None:
        await self.service.favorite(self.media_id)
        await self.service.sync_pending()
        await self.service.unfavorite(self.media_id)
        result = await self.service.sync_pending()
        self.assertEqual((result.synced, result.failed, result.retried), (1, 0, 0))
        self.assertEqual(self.writer.files, {})
        self.assertEqual(await self.repo.get_favorite_location(self.media_id), None)
        self.assertEqual(await self.repo.list_pending_favorite_sync(), [])
        self.assertEqual(self.events[-2:], ["delete", "tombstone"])

    async def test_delete_waits_until_remote_tombstone_is_persisted(self) -> None:
        await self.service.favorite(self.media_id)
        await self.service.sync_pending()
        self.snapshot_exporter.fail = True
        with self.assertRaises(Exception):
            await self.service.unfavorite(self.media_id)
        blocked = await self.service.sync_pending()
        self.assertEqual(blocked.processed, 0)
        self.assertTrue(self.writer.files)

        self.snapshot_exporter.fail = False
        await self.service.unfavorite(self.media_id)
        deleted = await self.service.sync_pending()
        self.assertEqual(deleted.synced, 1)
        self.assertEqual(self.writer.files, {})


if __name__ == "__main__":
    unittest.main()
