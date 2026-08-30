import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.repository import SQLiteRepository
from src.services.shadow_state import ShadowState


class ShadowStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.downloads = root / "downloads"
        self.downloads.mkdir()
        self.repo = SQLiteRepository(root / "state.sqlite3", download_root=self.downloads)
        await self.repo.open()
        await self.repo.migrate()
        self.shadow = ShadowState(self.repo)

    async def asyncTearDown(self) -> None:
        await self.shadow.drain()
        await self.repo.close()

    @staticmethod
    def message(mid: int, grouped_id: int | None = None):
        document = SimpleNamespace(file_name=f"{mid}.mp4", mime_type="video/mp4")
        media = SimpleNamespace(document=document, photo=None)
        return SimpleNamespace(id=mid, chat_id=99, grouped_id=grouped_id, media=media)

    async def test_collection_order_transitions_and_published_refs(self) -> None:
        self.shadow.accept(
            500,
            kind="collection",
            user_id=42,
            state="awaiting_confirmation",
            source_kind="telegram",
            album=[self.message(1, 7), self.message(2, 7)],
            texts=["alpha", "beta"],
            spoiler=False,
        )
        self.shadow.transition(500, "confirmed", "queued", spoiler=True)
        self.shadow.published(500, [(-1001, 10), (-1002, 20)])
        await self.shadow.drain()

        job_id = self.shadow.job_ids[500]
        items = await self.repo.list_job_items(job_id)
        self.assertEqual([item.source_message_id for item in items], [1, 2])
        self.assertEqual(await self.repo.list_job_texts(job_id), ["alpha", "beta"])
        events = await self.repo.list_job_events(job_id)
        self.assertEqual([event.event_type for event in events], ["accepted", "confirmed", "published"])
        refs = await self.repo.list_published_messages(job_id)
        self.assertEqual([(ref.peer_id, ref.message_id) for ref in refs], [(-1001, 10), (-1002, 20)])
        self.assertEqual((await self.repo.get_job(job_id)).state, "succeeded")

    async def test_album_merge_cancel_retry_and_webdav_attempt_mapping(self) -> None:
        self.shadow.accept(
            600,
            kind="album",
            user_id=42,
            state="queued",
            source_kind="telegram",
            album=[self.message(11, 8), self.message(12, 8)],
            texts=[],
            spoiler=False,
        )
        self.shadow.accept(
            600,
            kind="album",
            user_id=42,
            state="queued",
            source_kind="telegram",
            album=[self.message(12, 8), self.message(13, 8)],
            texts=[],
            spoiler=False,
        )
        self.shadow.transition(600, "cancelled", "cancelled")
        self.shadow.transition(600, "retry_requested", "failed", retry_seq=601)
        await self.shadow.drain()

        job_id = self.shadow.job_ids[600]
        items = await self.repo.list_job_items(job_id)
        self.assertEqual([item.source_message_id for item in items], [11, 12, 13])
        events = await self.repo.list_job_events(job_id)
        self.assertEqual(events[-2].event_type, "cancelled")
        self.assertEqual(events[-1].event_type, "retry_requested")

        cache = self.downloads / "job-600"
        cache.mkdir()
        local = cache / "a.mp4"
        local.write_bytes(b"abc")
        self.shadow.backup_started(
            600,
            "backup/2026-08-30/1",
            [{"local": str(local), "name": "hash.mp4", "size": os.path.getsize(local)}],
        )
        await self.shadow.drain()
        attempts = await self.repo.list_backup_attempts(job_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].remote_dir, "backup/2026-08-30/1")
        files = await self.repo.list_backup_files(attempts[0].id)
        self.assertEqual([(f.remote_name, f.size_bytes) for f in files], [("hash.mp4", 3)])

