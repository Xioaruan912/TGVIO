import tempfile
import unittest
from pathlib import Path

from src.repository import SQLiteRepository
from src.services.dedup import DedupManager, sha256_file


class DedupHashTests(unittest.TestCase):
    def test_same_content_different_name_has_same_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.mp4"
            second = root / "renamed.bin"
            payload = b"same-content" * 1024
            first.write_bytes(payload)
            second.write_bytes(payload)
            a = sha256_file(str(first), chunk_size=4096)
            b = sha256_file(str(second), chunk_size=4096)
        self.assertEqual(a.sha256, b.sha256)
        self.assertEqual(a.size_bytes, len(payload))

    def test_same_size_different_content_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a.bin"
            second = root / "b.bin"
            first.write_bytes(b"A" * 8192)
            second.write_bytes(b"B" * 8192)
            a = sha256_file(str(first))
            b = sha256_file(str(second))
        self.assertEqual(a.size_bytes, b.size_bytes)
        self.assertNotEqual(a.sha256, b.sha256)


class DedupRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.downloads = root / "downloads"
        self.downloads.mkdir()
        self.repo = SQLiteRepository(root / "state.sqlite3", download_root=self.downloads)
        await self.repo.open()
        await self.repo.migrate()

    async def asyncTearDown(self) -> None:
        await self.repo.close()

    async def test_download_hash_is_persisted_by_item_ordinal(self) -> None:
        job = await self.repo.accept_job(
            kind="url", user_id=1, state="queued", source_kind="url", legacy_seq=501,
            source_url="https://example.invalid/item", event_payload={"schema_version": 1},
            items=[{"media_kind": "video"}],
        )
        job_dir = self.downloads / "job-501"
        job_dir.mkdir()
        path = job_dir / "media.mp4"
        path.write_bytes(b"payload" * 100)
        manager = DedupManager(self.repo, destination_key="@dest-a")
        hashes = await manager.hash_job_paths(501, str(path))
        items = await self.repo.list_job_items(job.id)
        self.assertEqual(len(hashes), 1)
        self.assertEqual(items[0].content_sha256, hashes[0].sha256)
        self.assertEqual(items[0].size_bytes, hashes[0].size_bytes)

    async def test_lookup_is_destination_scoped(self) -> None:
        digest = "ab" * 32
        await self.repo.upsert_dedup_entry(
            sha256=digest, size_bytes=123, media_kind="video", destination_key="@dest-a",
            source_peer_id=-1001, source_message_id=55, media_id=99, access_hash=101,
            file_reference=b"ref", metadata={"schema_version": 1}, now=100.0,
        )
        hit = await self.repo.lookup_dedup_entry(
            sha256=digest, size_bytes=123, media_kind="video", destination_key="@dest-a"
        )
        miss = await self.repo.lookup_dedup_entry(
            sha256=digest, size_bytes=123, media_kind="video", destination_key="@dest-b"
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.source_message_id, 55)
        self.assertIsNone(miss)

    async def test_upsert_refreshes_descriptor_without_duplicate_row(self) -> None:
        digest = "cd" * 32
        first = await self.repo.upsert_dedup_entry(
            sha256=digest, size_bytes=456, media_kind="document", destination_key="@dest",
            source_peer_id=-1001, source_message_id=1, now=100.0,
        )
        second = await self.repo.upsert_dedup_entry(
            sha256=digest, size_bytes=456, media_kind="document", destination_key="@dest",
            source_peer_id=-1001, source_message_id=2, now=200.0,
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.source_message_id, 2)
        self.assertEqual(second.verified_at, 200.0)


if __name__ == "__main__":
    unittest.main()
