import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.repository import SQLiteRepository
from src.services.recovery import recover_jobs


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
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

    async def _url_job(self, state: str, seq: int):
        return await self.repo.accept_job(
            kind="url",
            user_id=42,
            state=state,
            source_kind="url",
            source_url=f"https://example.invalid/{seq}",
            legacy_seq=seq,
            event_payload={"schema_version": 1, "legacy_seq": seq},
        )

    async def _ready_job(self, seq: int):
        job = await self._url_job("queued", seq)
        claim = await self.repo.claim_next_download(f"worker-{seq}")
        self.assertIsNotNone(claim)
        path = self.downloads / f"job-{seq}" / "media.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload")
        result = await self.repo.record_download_ready(
            job.id,
            [str(path)],
            expected_revision=claim.job.revision,
        )
        self.assertTrue(result.applied)
        return await self.repo.get_job(job.id), path

    async def test_queued_url_is_recoverable_but_telegram_source_fails_closed(self) -> None:
        url_job = await self._url_job("queued", 1001)
        telegram_job = await self.repo.accept_job(
            kind="media",
            user_id=42,
            state="queued",
            source_kind="telegram",
            legacy_seq=1002,
            items=[{"source_chat_id": 42, "source_message_id": 9, "metadata": {"schema_version": 1}}],
            event_payload={"schema_version": 1, "legacy_seq": 1002},
        )
        actions = await recover_jobs(self.repo)
        by_id = {action.job.id: action for action in actions}
        self.assertEqual(by_id[url_job.id].action, "download")
        self.assertEqual(by_id[telegram_job.id].action, "failed")
        self.assertEqual((await self.repo.get_job(telegram_job.id)).state, "failed")

    async def test_downloading_url_becomes_interrupted_then_requeued(self) -> None:
        job = await self._url_job("queued", 1101)
        claim = await self.repo.claim_next_download("worker-a")
        self.assertEqual(claim.job.id, job.id)
        actions = await recover_jobs(self.repo)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "download")
        current = await self.repo.get_job(job.id)
        self.assertEqual(current.state, "queued")
        events = await self.repo.list_job_events(job.id)
        self.assertEqual(events[-2].event_type, "recovery_interrupted")
        self.assertEqual(events[-1].event_type, "recovery_requeued")

    async def test_ready_cache_exists_recovers_publish_and_missing_cache_fails(self) -> None:
        ready, path = await self._ready_job(1201)
        actions = await recover_jobs(self.repo)
        action = next(a for a in actions if a.job.id == ready.id)
        self.assertEqual(action.action, "publish")
        self.assertEqual(action.paths, (str(path.resolve()),))

        missing, missing_path = await self._ready_job(1202)
        os.remove(missing_path)
        actions = await recover_jobs(self.repo)
        action = next(a for a in actions if a.job.id == missing.id)
        self.assertEqual(action.action, "failed")
        self.assertEqual((await self.repo.get_job(missing.id)).state, "failed")

    async def test_publishing_without_refs_retries_ready_but_partial_refs_fail_closed(self) -> None:
        safe, safe_path = await self._ready_job(1301)
        claim = await self.repo.claim_next_publish("publisher")
        self.assertEqual(claim.job.id, safe.id)
        actions = await recover_jobs(self.repo)
        action = next(a for a in actions if a.job.id == safe.id)
        self.assertEqual(action.action, "publish")
        self.assertEqual(action.paths, (str(safe_path.resolve()),))
        self.assertEqual((await self.repo.get_job(safe.id)).state, "ready")
        current = await self.repo.get_job(safe.id)
        await self.repo.transition_job(
            safe.id,
            expected_revision=current.revision,
            to_state="cancelled",
            event_type="test_settled",
            payload={"schema_version": 1},
        )

        partial, _ = await self._ready_job(1302)
        claim = await self.repo.claim_next_publish("publisher-2")
        self.assertEqual(claim.job.id, partial.id)
        raw = sqlite3.connect(self.repo.path)
        try:
            raw.execute("PRAGMA foreign_keys=ON")
            raw.execute(
                "INSERT INTO published_messages(job_id,peer_id,message_id,role,created_at) VALUES (?,?,?,?,?)",
                (partial.id, -1001, 99, "partial", 1.0),
            )
            raw.commit()
        finally:
            raw.close()
        actions = await recover_jobs(self.repo)
        action = next(a for a in actions if a.job.id == partial.id)
        self.assertEqual(action.action, "failed")
        self.assertEqual(action.reason, "published_refs_exist_manual_review")
        self.assertEqual((await self.repo.get_job(partial.id)).state, "failed")

