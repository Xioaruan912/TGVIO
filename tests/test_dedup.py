import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import types

from src.media import MediaPublisher
from src.models import Job
from src.repository import SQLiteRepository
from src.services.dedup import ContentHash, DedupManager, sha256_file
from tests.fakes import FakeStatusMessage


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
        self.assertEqual(a.md5_short, b.md5_short)
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

    async def test_reuse_refreshes_destination_message_and_marks_saved_bytes(self) -> None:
        digest = "ef" * 32
        entry = await self.repo.upsert_dedup_entry(
            sha256=digest, size_bytes=321, media_kind="document", destination_key="@dest",
            source_peer_id=-100123, source_message_id=55, now=100.0,
        )
        document = types.Document(
            id=99,
            access_hash=101,
            file_reference=b"fresh-ref",
            date=datetime.now(),
            mime_type="video/mp4",
            size=321,
            dc_id=2,
            attributes=[],
        )
        message = SimpleNamespace(
            id=55,
            peer_id=types.PeerChannel(123),
            media=types.MessageMediaDocument(document=document),
        )

        class _Client:
            async def get_messages(self, peer, *, ids):
                self.peer = peer
                self.ids = ids
                return message

        manager = DedupManager(self.repo, destination_key="@dest")
        content = ContentHash("/tmp/item.mp4", digest, 321)
        media, hit = await manager.reuse_input_media(_Client(), content, spoiler=True)
        self.assertIsInstance(media, types.InputMediaDocument)
        self.assertEqual(hit.id, entry.id)
        self.assertTrue(media.spoiler)

        await manager.mark_hit(hit, content)
        refreshed = await self.repo.lookup_dedup_entry(
            sha256=digest, size_bytes=321, media_kind="document", destination_key="@dest"
        )
        self.assertEqual(refreshed.hit_count, 1)
        stats = await self.repo.stats_snapshot()
        self.assertEqual(stats["totals"]["saved_upload_bytes"], 321)

    async def test_stale_destination_reference_is_removed_and_returns_miss(self) -> None:
        digest = "12" * 32
        entry = await self.repo.upsert_dedup_entry(
            sha256=digest, size_bytes=99, media_kind="document", destination_key="@dest",
            source_peer_id=-100123, source_message_id=77, now=100.0,
        )

        class _Client:
            async def get_messages(self, _peer, *, ids):
                raise RuntimeError(f"missing {ids}")

        manager = DedupManager(self.repo, destination_key="@dest")
        media, hit = await manager.reuse_input_media(
            _Client(), ContentHash("/tmp/item.mp4", digest, 99)
        )
        self.assertIsNone(media)
        self.assertIsNone(hit)
        self.assertIsNone(
            await self.repo.lookup_dedup_entry(
                sha256=digest, size_bytes=99, media_kind="document", destination_key="@dest"
            )
        )

    async def test_same_content_renamed_reuses_destination_media_but_sends_new_caption(self) -> None:
        class _Client:
            def __init__(self):
                self.next_id = 100
                self.messages = {}
                self.sent = []

            async def send_file(self, peer, file, **kwargs):
                self.next_id += 1
                document = types.Document(
                    id=self.next_id,
                    access_hash=self.next_id + 1000,
                    file_reference=f"ref-{self.next_id}".encode(),
                    date=datetime.now(),
                    mime_type="video/mp4",
                    size=777,
                    dc_id=2,
                    attributes=[],
                )
                message = SimpleNamespace(
                    id=self.next_id,
                    peer_id=types.PeerChannel(123),
                    media=types.MessageMediaDocument(document=document),
                )
                self.messages[message.id] = message
                self.sent.append({"peer": peer, "file": file, "caption": kwargs.get("caption")})
                return message

            async def get_messages(self, _peer, *, ids):
                return self.messages[int(ids)]

        job_dir1 = self.downloads / "job-601"
        job_dir2 = self.downloads / "job-602"
        job_dir1.mkdir()
        job_dir2.mkdir()
        first = job_dir1 / "first-name.mp4"
        second = job_dir2 / "renamed-video.mp4"
        payload = b"same-video" * 77
        first.write_bytes(payload)
        second.write_bytes(payload)
        first_hash = sha256_file(str(first))
        second_hash = sha256_file(str(second))
        self.assertEqual(first_hash.sha256, second_hash.sha256)

        client = _Client()
        manager = DedupManager(self.repo, destination_key="@dest")
        publisher = MediaPublisher(
            client,
            "@dest",
            lambda seq: str(self.downloads / f"job-{seq}"),
            upload_timeout=30,
            max_file_size=10_000,
            forward_caption=True,
            cover_mode=False,
        )
        publisher.dedup_manager = manager
        publisher._get_dest_input = AsyncMock(return_value="dest-input")
        publisher._upload_media_input = AsyncMock(return_value="uploaded-media")

        first_job = Job(
            seq=601,
            kind="media",
            status=FakeStatusMessage(),
            message=SimpleNamespace(message="第一次文案"),
        )
        first_job._content_hashes = {str(first.resolve()): first_hash}
        await publisher._publish_media(first_job, str(first))
        publisher._upload_media_input.assert_awaited_once()

        publisher._upload_media_input.reset_mock()
        publisher._upload_media_input.side_effect = AssertionError("dedup hit must not upload bytes")
        second_job = Job(
            seq=602,
            kind="media",
            status=FakeStatusMessage(),
            message=SimpleNamespace(message="第二次新文案"),
        )
        second_job._content_hashes = {str(second.resolve()): second_hash}
        await publisher._publish_media(second_job, str(second))

        publisher._upload_media_input.assert_not_awaited()
        self.assertIsInstance(client.sent[1]["file"], types.InputMediaDocument)
        self.assertIn("第二次新文案", client.sent[1]["caption"])
        stats = await self.repo.stats_snapshot()
        self.assertEqual(stats["totals"]["saved_upload_bytes"], len(payload))

    async def test_marking_one_published_message_deleted_does_not_remove_dedup_index(self) -> None:
        digest = "34" * 32
        entry = await self.repo.upsert_dedup_entry(
            sha256=digest,
            size_bytes=222,
            media_kind="document",
            destination_key="@dest",
            source_peer_id=-100123,
            source_message_id=88,
            now=100.0,
        )
        job = await self.repo.accept_job(
            kind="url",
            user_id=1,
            state="publishing",
            source_kind="url",
            legacy_seq=700,
            source_url="https://example.invalid/reuse",
            event_payload={"schema_version": 1},
        )
        await self.repo.checkpoint_published_messages(
            job.id,
            [(-100123, 88, "destination")],
        )
        refs = await self.repo.list_published_messages(job.id)
        await self.repo.mark_published_deleted(
            job.id,
            expected_revision=job.revision,
            message_ids=[refs[0].id],
        )

        still_indexed = await self.repo.lookup_dedup_entry(
            sha256=digest,
            size_bytes=222,
            media_kind="document",
            destination_key="@dest",
        )
        self.assertIsNotNone(still_indexed)
        self.assertEqual(still_indexed.id, entry.id)


if __name__ == "__main__":
    unittest.main()
