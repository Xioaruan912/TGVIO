from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.adapters.telegram.bot_ui import TelethonBotUI
from tgvio.application.bulk_retry import BulkRetryService
from tgvio.application.operation_tokens import OperationTokenService
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
from tests.test_bot_ui import FakeClient, FakeEvent, settings


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


    def _ui(self, scheduled: list) -> TelethonBotUI:
        return TelethonBotUI(
            FakeClient(),
            settings(),
            self.repo,
            control=JobControlService(self.repo),
            schedule_job=lambda job, chat_id: scheduled.append(job.id),
            operation_tokens=OperationTokenService(self.repo),
            bulk_retry=self.service,
        )

    async def test_one_tap_confirms_then_schedules_everything(self) -> None:
        failed = await self._failed_download("download_failed")
        await self._succeeded_with_skipped("telegram_file_timeout")
        scheduled: list[str] = []
        ui = self._ui(scheduled)

        page = FakeEvent(data=b"ui:failures:0")
        await ui._on_callback(page)
        buttons = [button for row in page.edits[-1][1]["buttons"] for button in row]
        self.assertIn(b"ui:retry-all", [button.data for button in buttons])

        ask = FakeEvent(data=b"ui:retry-all")
        await ui._on_callback(ask)
        text, kwargs = ask.edits[-1]
        self.assertIn("重新处理 1 个失败的任务", text)
        self.assertIn("补发 1 个之前没下载到的文件", text)
        confirm = kwargs["buttons"][0][0].data

        go = FakeEvent(data=confirm)
        await ui._on_callback(go)
        self.assertIn("已开始重试 2 项", go.edits[-1][0])
        self.assertEqual(len(scheduled), 2)
        self.assertIn(failed.id, scheduled)

        again = FakeEvent(data=confirm)
        await ui._on_callback(again)
        self.assertIn("情况已经变了", again.edits[-1][0])
        self.assertEqual(len(scheduled), 2)


if __name__ == "__main__":
    unittest.main()
