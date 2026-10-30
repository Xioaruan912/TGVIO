from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.bulk_retry import BulkRetryService
from tgvio.application.item_recovery import SkippedItemRecoveryService
from tgvio.application.job_control import JobControlService
from tgvio.domain.job import (
    DOWNLOAD_SKIPPED_CODE_KEY,
    DOWNLOAD_SKIPPED_KEY,
    Job,
    JobState,
    MediaItem,
    MediaKind,
)
from tgvio.infrastructure.sqlite import SQLiteJobRepository


OWNER = 42


class BulkRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.service = BulkRetryService(
            self.repo,
            JobControlService(self.repo),
            item_recovery=SkippedItemRecoveryService(self.repo),
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _failed_download(self, code: str, *, owner_id: int = OWNER) -> Job:
        job = Job(
            owner_id=owner_id,
            destination="@channel",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        await self.repo.create(job)
        await self.repo.transition(job.id, JobState.DOWNLOADING, event_type="download_started")
        return await self.repo.transition(
            job.id,
            JobState.FAILED,
            event_type="download_failed",
            error_code=code,
            error_message="fixture failure",
        )

    async def _succeeded_with_skipped(self, *codes: str) -> Job:
        items = [MediaItem(index=0, kind=MediaKind.PHOTO, source="telegram:1:10")]
        for offset, code in enumerate(codes, start=1):
            items.append(
                MediaItem(
                    index=offset,
                    kind=MediaKind.VIDEO,
                    source=f"telegram:1:{10 + offset}",
                    source_chat_id=1,
                    source_message_id=10 + offset,
                    metadata={DOWNLOAD_SKIPPED_KEY: True, DOWNLOAD_SKIPPED_CODE_KEY: code},
                )
            )
        job = Job(owner_id=OWNER, destination="@channel", state=JobState.SUCCEEDED, items=items)
        await self.repo.create(job)
        return job

    async def test_plan_collects_every_safe_retry_and_blocks_the_rest(self) -> None:
        retryable = await self._failed_download("download_failed")
        deleted = await self._failed_download("source_missing")
        uncertain = await self._failed_download("publish_uncertain")
        await self._failed_download("download_failed", owner_id=7)
        skipped = await self._succeeded_with_skipped("telegram_file_timeout", "download_failed")
        await self._succeeded_with_skipped("source_missing")

        plan = await self.service.plan(OWNER)

        self.assertEqual(plan.job_ids, (retryable.id,))
        self.assertEqual(set(plan.blocked_job_ids), {deleted.id, uncertain.id})
        self.assertEqual(plan.skipped_parent_ids, (skipped.id,))
        self.assertEqual(plan.skipped_items, 2)

    async def test_old_job_whose_media_was_all_deleted_is_not_retried(self) -> None:
        # Before source_missing existed, such Jobs were recorded as download_failed.
        job = Job(
            owner_id=OWNER,
            destination="@channel",
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.VIDEO,
                    source="telegram:1:10",
                    metadata={DOWNLOAD_SKIPPED_KEY: True, DOWNLOAD_SKIPPED_CODE_KEY: "source_missing"},
                )
            ],
        )
        await self.repo.create(job)
        await self.repo.transition(job.id, JobState.DOWNLOADING, event_type="download_started")
        await self.repo.transition(
            job.id, JobState.FAILED, event_type="download_failed", error_code="download_failed"
        )

        plan = await self.service.plan(OWNER)

        self.assertEqual(plan.job_ids, ())
        self.assertEqual(plan.blocked_job_ids, (job.id,))

    async def test_run_retries_jobs_and_recovers_skipped_items_once(self) -> None:
        failed = await self._failed_download("download_failed")
        parent = await self._succeeded_with_skipped("telegram_file_timeout")

        result = await self.service.run(OWNER, await self.service.plan(OWNER))

        self.assertEqual([decision.job.id for decision in result.retried], [failed.id])
        self.assertEqual((await self.repo.get(failed.id)).state, JobState.RECEIVED)
        self.assertEqual(len(result.recovery_children), 1)
        self.assertEqual(len(result.recovery_children[0].items), 1)
        self.assertTrue((await self.service.plan(OWNER)).empty)
        self.assertNotEqual(result.recovery_children[0].id, parent.id)

    async def test_stale_plan_entries_are_counted_not_forced(self) -> None:
        failed = await self._failed_download("download_failed")
        plan = await self.service.plan(OWNER)
        await JobControlService(self.repo).retry_failed(await self.repo.get(failed.id))

        result = await self.service.run(OWNER, plan)

        self.assertEqual(result.retried, [])
        self.assertEqual(result.unchanged, 1)


if __name__ == "__main__":
    unittest.main()
