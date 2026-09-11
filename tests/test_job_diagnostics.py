from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tgvio.application.job_diagnostics import JobDiagnosticService
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.publish import PublishPlan, PublishStep, PublishStepKind, PublishStepState, PublishTarget
from tgvio.infrastructure.log_reader import JsonlOperationalLogReader


class JobDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_uncertain_produces_manual_review_hint(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.FAILED,
            error_code="publish_uncertain",
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="fixture")],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            state=PublishStepState.FAILED,
            error_code="publish_uncertain",
        )
        plan = PublishPlan(job_id=job.id, steps=(step,), summary={})
        repo = SimpleNamespace(
            list_events=AsyncMock(return_value=[]),
            get_job_progress=AsyncMock(return_value=None),
            get_publish_plan=AsyncMock(return_value=plan),
            list_publish_effects=AsyncMock(return_value=[]),
            get_archive_package_for_job=AsyncMock(return_value=None),
        )
        snapshot = await JobDiagnosticService(repo).inspect(job)
        self.assertEqual(snapshot.hints[0].code, "publish_uncertain")
        self.assertIn("盲目重发", snapshot.hints[0].summary)

    async def test_archive_failure_is_reported_separately_from_successful_publish(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            items=[MediaItem(index=0, kind=MediaKind.DOCUMENT, source="fixture")],
        )
        package = ArchivePackage(
            id=f"arc_{job.id}",
            job_id=job.id,
            layout_version="v1",
            remote_path="archive/x",
            staging_path=".staging/x",
            state=ArchivePackageState.FAILED,
            manifest={},
            objects=(),
            error_code="archive_execution_failed",
        )
        repo = SimpleNamespace(
            list_events=AsyncMock(return_value=[]),
            get_job_progress=AsyncMock(return_value=None),
            get_publish_plan=AsyncMock(return_value=None),
            get_archive_package_for_job=AsyncMock(return_value=package),
            list_archive_events=AsyncMock(return_value=[]),
        )
        snapshot = await JobDiagnosticService(repo).inspect(job)
        self.assertTrue(any(hint.code == "archive_execution_failed" for hint in snapshot.hints))
        self.assertEqual(job.state, JobState.SUCCEEDED)

    async def test_log_reader_correlates_current_and_rotated_jsonl(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tgvio.jsonl.1").write_text(
                json.dumps(
                    {
                        "ts": "1",
                        "event": "download.job.started",
                        "job_id": "abc",
                        "source_url": "forbidden",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "tgvio.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "ts": "2",
                                "event": "publish.step.started",
                                "job_id": "abc",
                                "step_index": 0,
                            }
                        ),
                        json.dumps({"ts": "3", "event": "other", "job_id": "other"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            rows = await JsonlOperationalLogReader(root).recent_for_job("abc", limit=10)
            self.assertEqual(
                [row["event"] for row in rows],
                ["download.job.started", "publish.step.started"],
            )
            self.assertNotIn("source_url", rows[0])

    async def test_bot_method_invalid_log_gets_specific_root_cause_hint(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.FAILED,
            error_code="publish_failed",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        logs = SimpleNamespace(
            recent_for_job=AsyncMock(
                return_value=[
                    {
                        "event": "publish.job.failed",
                        "level": "ERROR",
                        "exception_type": "BotMethodInvalidError",
                    }
                ]
            )
        )
        repo = SimpleNamespace(
            list_events=AsyncMock(return_value=[]),
            get_job_progress=AsyncMock(return_value=None),
            get_publish_plan=AsyncMock(return_value=None),
            get_archive_package_for_job=AsyncMock(return_value=None),
        )
        snapshot = await JobDiagnosticService(repo, logs).inspect(job)
        self.assertEqual(snapshot.hints[0].code, "telegram_bot_method_invalid")

