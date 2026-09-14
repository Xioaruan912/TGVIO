from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.collection_editing import (
    CollectionEditingService,
    DraftUnavailableError,
)
from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.operation_tokens import (
    OperationTokenInvalidError,
    OperationTokenService,
)
from tgvio.domain.collection_editing import DraftState
from tgvio.domain.intake import SpoilerMode
from tgvio.domain.job import MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


def _media(message_id: int) -> IncomingMedia:
    return IncomingMedia(
        kind=MediaKind.VIDEO,
        source=f"telegram:42:{message_id}",
        source_chat_id=42,
        source_message_id=message_id,
        size_bytes=1024,
        name=f"clip-{message_id}.mp4",
    )


class FrozenSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_submission_rejects_new_media_and_recovers_without_token(self):
        from tgvio.domain.intake import CollectionAlreadySubmittedError
        session, draft = await self._session(1)
        token = await self.editing.issue_confirm(
            owner_id=7, session_id=session.id, expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        original = self.editing._resume_submission
        async def crash(submission):
            raise RuntimeError("injected interruption")
        self.editing._resume_submission = crash
        with self.assertRaises(RuntimeError):
            await self.editing.confirm(owner_id=7, chat_id=42, token=token,
                                      destination="@channel", max_items=100, ask_timeout_seconds=60)
        with self.assertRaises(CollectionAlreadySubmittedError):
            await self.intake.add_collection_media(session, [_media(9999)])
        with self.assertRaises(CollectionAlreadySubmittedError):
            await self.intake.add_collection_text(session, text="late", source_chat_id=42,
                                                  source_message_id=10000)
        self.editing._resume_submission = original
        await self.repo.close()
        await self.repo.open()
        pending = await self.repo.page_pending_submissions()
        self.assertEqual([s.session_id for s in pending], [session.id])
        import asyncio
        from unittest.mock import AsyncMock
        from tests.test_intake_runtime_collections import FakeClient, RecordingRunner, settings
        from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime
        runtime = TelethonIntakeRuntime(FakeClient(), settings(), self.intake,
                                       RecordingRunner(), editing=self.editing)
        runtime._announce_finalize_result = AsyncMock()
        await runtime.start()
        try:
            for _ in range(100):
                if not await self.repo.page_pending_submissions():
                    break
                await asyncio.sleep(0.01)
        finally:
            await runtime.stop()
        jobs = await self.repo.list_recent(owner_id=7, limit=10)
        self.assertEqual(sum(len(job.items) for job in jobs), 1)
        self.assertEqual(await self.repo.page_pending_submissions(), [])
        self.assertIsNone(await self.editing.recover_submission(session.id))

    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.repo = SQLiteJobRepository(self.path)
        await self.repo.open()
        self.intake = IntakeService(self.repo)
        self.editing = CollectionEditingService(
            self.repo, self.intake, OperationTokenService(self.repo)
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _session(self, count: int):
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        await self.intake.add_collection_media(
            session, [_media(100 + index) for index in range(count)]
        )
        draft = await self.editing.draft(session.id)
        assert draft is not None
        return session, draft

    async def test_commit_persists_frozen_snapshot_and_token(self) -> None:
        session, draft = await self._session(3)
        token = await self.editing.issue_confirm(
            owner_id=7,
            session_id=session.id,
            expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        result = await self.editing.confirm(
            owner_id=7,
            chat_id=42,
            token=token,
            destination="@channel",
            max_items=100,
            ask_timeout_seconds=60,
        )
        self.assertEqual(len(result.jobs), 1)
        submission = await self.repo.get_submission(session.id)
        assert submission is not None
        self.assertEqual(submission.state, "created")
        self.assertEqual(submission.token_id, token)
        self.assertIn('"parts"', submission.frozen_json or "")
        draft = await self.editing.draft(session.id)
        assert draft is not None
        self.assertEqual(draft.state, DraftState.SUBMITTED)

    async def test_edit_after_submission_is_blocked(self) -> None:
        session, draft = await self._session(2)
        token = await self.editing.issue_confirm(
            owner_id=7,
            session_id=session.id,
            expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        await self.editing.confirm(
            owner_id=7, chat_id=42, token=token, destination="@channel",
            max_items=100, ask_timeout_seconds=60,
        )
        entries = await self.editing.entries(session.id)
        with self.assertRaises(DraftUnavailableError):
            await self.editing.toggle_exclude(
                owner_id=7,
                session_id=session.id,
                entry_id=int(entries[0].entry.id),
                expected_revision=draft.revision,
            )

    async def test_consumed_token_resumes_without_duplicate_jobs(self) -> None:
        session, draft = await self._session(2)
        token = await self.editing.issue_confirm(
            owner_id=7,
            session_id=session.id,
            expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        original = self.repo.record_collection_part_job
        state = {"calls": 0}

        async def flaky(session_id, part_index, job_id):
            state["calls"] += 1
            if state["calls"] >= 2:
                raise RuntimeError("injected crash after first part")
            return await original(session_id, part_index, job_id)

        self.repo.record_collection_part_job = flaky  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            await self.editing.confirm(
                owner_id=7, chat_id=42, token=token, destination="@channel",
                max_items=1, ask_timeout_seconds=60,
            )
        self.repo.record_collection_part_job = original  # type: ignore[assignment]
        self.assertEqual(
            await self.repo.get_collection_part_job(session.id, 0) is not None, True
        )

        # Same (now consumed) token must resume the exact frozen submission.
        resumed = await self.editing.confirm(
            owner_id=7, chat_id=42, token=token, destination="@channel",
            max_items=1, ask_timeout_seconds=60,
        )
        self.assertEqual(len(resumed.jobs), 2)
        jobs = await self.repo.list_recent(owner_id=7, limit=10)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            {int(job.policy.get("collection_part_index", -1)) for job in jobs}, {0, 1}
        )
        submission = await self.repo.get_submission(session.id)
        assert submission is not None
        self.assertEqual(submission.state, "created")
        self.assertEqual(len(submission.job_ids), 2)

    async def test_crash_before_created_resumes_without_duplicates(self) -> None:
        session, draft = await self._session(2)
        token = await self.editing.issue_confirm(
            owner_id=7,
            session_id=session.id,
            expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        original = self.repo.finish_submission

        async def flaky_finish(*args, **kwargs):
            raise RuntimeError("injected crash before created")

        self.repo.finish_submission = flaky_finish  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            await self.editing.confirm(
                owner_id=7, chat_id=42, token=token, destination="@channel",
                max_items=1, ask_timeout_seconds=60,
            )
        self.repo.finish_submission = original  # type: ignore[assignment]

        resumed = await self.editing.confirm(
            owner_id=7, chat_id=42, token=token, destination="@channel",
            max_items=1, ask_timeout_seconds=60,
        )
        self.assertEqual(len(resumed.jobs), 2)
        self.assertEqual(len(await self.repo.list_recent(owner_id=7, limit=10)), 2)

    async def test_second_valid_token_cannot_create_second_submission(self) -> None:
        session, draft = await self._session(2)
        first = await self.editing.issue_confirm(
            owner_id=7, session_id=session.id, expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        second = await self.editing.issue_confirm(
            owner_id=7, session_id=session.id, expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        await self.editing.confirm(
            owner_id=7, chat_id=42, token=first, destination="@channel",
            max_items=100, ask_timeout_seconds=60,
        )
        with self.assertRaises(OperationTokenInvalidError):
            await self.editing.confirm(
                owner_id=7, chat_id=42, token=second, destination="@channel",
                max_items=100, ask_timeout_seconds=60,
            )
        self.assertEqual(len(await self.repo.list_recent(owner_id=7, limit=10)), 1)

    async def test_owner_mismatch_and_missing_token_fail_closed(self) -> None:
        session, draft = await self._session(1)
        token = await self.editing.issue_confirm(
            owner_id=7, session_id=session.id, expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        with self.assertRaises(OperationTokenInvalidError):
            await self.editing.confirm(
                owner_id=9, chat_id=42, token=token, destination="@channel",
                max_items=100, ask_timeout_seconds=60,
            )
        with self.assertRaises(OperationTokenInvalidError):
            await self.editing.confirm(
                owner_id=7, chat_id=42, token="not-a-token", destination="@channel",
                max_items=100, ask_timeout_seconds=60,
            )
        self.assertEqual(await self.repo.list_recent(owner_id=7, limit=10), [])

    async def test_tampered_frozen_snapshot_is_rejected(self) -> None:
        session, draft = await self._session(2)
        token = await self.editing.issue_confirm(
            owner_id=7, session_id=session.id, expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        original = self.repo.finish_submission

        async def flaky_finish(*args, **kwargs):
            raise RuntimeError("crash")

        self.repo.finish_submission = flaky_finish  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            await self.editing.confirm(
                owner_id=7, chat_id=42, token=token, destination="@channel",
                max_items=1, ask_timeout_seconds=60,
            )
        self.repo.finish_submission = original  # type: ignore[assignment]
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                "UPDATE collection_submissions SET frozen_json='{}' WHERE session_id=?",
                (session.id,),
            )
        with self.assertRaises(DraftUnavailableError):
            await self.editing.confirm(
                owner_id=7, chat_id=42, token=token, destination="@channel",
                max_items=1, ask_timeout_seconds=60,
            )



    async def test_concurrent_confirm_two_connections_creates_one_submission(self) -> None:
        import asyncio

        from tgvio.application.operation_tokens import OperationTokenService

        session, draft = await self._session(2)
        first = await self.editing.issue_confirm(
            owner_id=7, session_id=session.id, expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        second = await self.editing.issue_confirm(
            owner_id=7, session_id=session.id, expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        repo2 = SQLiteJobRepository(self.path)
        await repo2.open()
        try:
            editing2 = CollectionEditingService(
                repo2, IntakeService(repo2), OperationTokenService(repo2)
            )

            async def run(service, token):
                try:
                    await service.confirm(
                        owner_id=7, chat_id=42, token=token, destination="@channel",
                        max_items=100, ask_timeout_seconds=60,
                    )
                    return "ok"
                except OperationTokenInvalidError:
                    return "invalid"

            results = await asyncio.gather(
                run(self.editing, first), run(editing2, second)
            )
        finally:
            await repo2.close()
        self.assertEqual(sorted(results), ["invalid", "ok"])
        self.assertEqual(len(await self.repo.list_recent(owner_id=7, limit=10)), 1)
        submission = await self.repo.get_submission(session.id)
        assert submission is not None
        self.assertEqual(submission.state, "created")


if __name__ == "__main__":
    unittest.main()
