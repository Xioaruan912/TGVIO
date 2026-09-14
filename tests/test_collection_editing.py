from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.collection_editing import (
    CollectionEditingService,
    DraftRevisionConflict,
    DraftUnavailableError,
)
from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.operation_tokens import OperationTokenService
from tgvio.domain.collection_editing import DraftState
from tgvio.domain.intake import SpoilerMode
from tgvio.domain.job import MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


def _media(message_id: int, kind: MediaKind = MediaKind.VIDEO) -> IncomingMedia:
    return IncomingMedia(
        kind=kind,
        source=f"telegram:42:{message_id}",
        source_chat_id=42,
        source_message_id=message_id,
        size_bytes=1024,
        name=f"clip-{message_id}.mp4",
    )


class CollectionEditingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_confirmation_payload_never_creates_job(self) -> None:
        from tgvio.application.operation_tokens import OperationTokenInvalidError
        session, draft = await self._session_with(1)
        token = await self.editing.issue_confirm(
            owner_id=7, session_id=session.id, expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.ALWAYS_NORMAL, style_policy={"cover_mode": True},
        )
        with self.assertRaises(OperationTokenInvalidError):
            await self.editing.confirm(
                owner_id=7, chat_id=42, token=token, destination="@fixture",
                max_items=100, ask_timeout_seconds=60, style_policy={"cover_mode": False},
            )
        self.assertEqual(await self.repo.list_recent(owner_id=7), [])
        self.assertIsNone(await self.repo.get_submission(session.id))

    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.intake = IntakeService(self.repo)
        self.tokens = OperationTokenService(self.repo)
        self.editing = CollectionEditingService(self.repo, self.intake, self.tokens)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _session_with(self, count: int, *, kind: MediaKind = MediaKind.VIDEO):
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        await self.intake.add_collection_media(
            session, [_media(100 + index, kind) for index in range(count)]
        )
        draft = await self.editing.draft(session.id)
        assert draft is not None
        return session, draft

    async def test_begin_creates_active_draft_and_lists_it(self) -> None:
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        draft = await self.editing.draft(session.id)
        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft.revision, 1)
        self.assertEqual(draft.state, DraftState.COLLECTING)
        drafts = await self.repo.list_drafts(7)
        self.assertEqual([item.session_id for item in drafts], [session.id])
        self.assertIsNotNone(await self.intake.open_collection(owner_id=7, chat_id=42))

    async def test_reorder_exclude_cover_bump_revision_and_order_overlay(self) -> None:
        session, draft = await self._session_with(3)
        entries = await self.editing.entries(session.id)
        self.assertEqual([item.position for item in entries], [0, 1, 2])
        first_id = int(entries[0].entry.id)
        second_id = int(entries[1].entry.id)
        third_id = int(entries[2].entry.id)

        moved = await self.editing.move(
            owner_id=7,
            session_id=session.id,
            entry_id=second_id,
            direction=-1,
            expected_revision=draft.revision,
        )
        self.assertEqual(moved.revision, 2)
        entries = await self.editing.entries(session.id)
        self.assertEqual(
            [int(item.entry.id) for item in entries], [second_id, first_id, third_id]
        )

        excluded = await self.editing.toggle_exclude(
            owner_id=7,
            session_id=session.id,
            entry_id=first_id,
            expected_revision=moved.revision,
        )
        self.assertEqual(excluded.revision, 3)
        entries = await self.editing.entries(session.id)
        target = next(item for item in entries if int(item.entry.id) == first_id)
        self.assertTrue(target.excluded)

        covered = await self.editing.select_cover(
            owner_id=7,
            session_id=session.id,
            entry_id=int(entries[0].entry.id),
            expected_revision=excluded.revision,
        )
        self.assertEqual(covered.revision, 4)
        self.assertEqual(covered.cover_entry_id, int(entries[0].entry.id))

        preview = await self.editing.preview(session_id=session.id)
        assert preview is not None
        self.assertEqual(preview.media_count, 2)

    async def test_stale_revision_is_rejected(self) -> None:
        session, draft = await self._session_with(2)
        entries = await self.editing.entries(session.id)
        first_id = int(entries[0].entry.id)
        await self.editing.toggle_exclude(
            owner_id=7,
            session_id=session.id,
            entry_id=first_id,
            expected_revision=draft.revision,
        )
        with self.assertRaises(DraftRevisionConflict):
            await self.editing.toggle_exclude(
                owner_id=7,
                session_id=session.id,
                entry_id=first_id,
                expected_revision=draft.revision,
            )

    async def test_cover_entry_must_be_visible_media(self) -> None:
        session, draft = await self._session_with(2)
        entries = await self.editing.entries(session.id)
        first_id = int(entries[0].entry.id)
        await self.editing.toggle_exclude(
            owner_id=7,
            session_id=session.id,
            entry_id=first_id,
            expected_revision=draft.revision,
        )
        with self.assertRaises(DraftUnavailableError):
            await self.editing.select_cover(
                owner_id=7,
                session_id=session.id,
                entry_id=first_id,
                expected_revision=draft.revision + 1,
            )

    async def test_caption_override_and_clear(self) -> None:
        session, draft = await self._session_with(1)
        interaction = await self.editing.begin_caption(
            owner_id=7,
            chat_id=42,
            session_id=session.id,
            expected_revision=draft.revision,
        )
        applied = await self.editing.apply_pending_caption(
            owner_id=7, chat_id=42, text="新文案\n第二行"
        )
        self.assertIsNotNone(applied)
        assert applied is not None
        self.assertEqual(applied.caption_override, "新文案\n第二行")
        self.assertIsNotNone(interaction.id)

        cleared = await self.editing.clear_caption(
            owner_id=7, session_id=session.id, expected_revision=applied.revision
        )
        self.assertIsNone(cleared.caption_override)

    async def test_confirm_freezes_snapshot_and_is_single_use_idempotent(self) -> None:
        session, draft = await self._session_with(3)
        entries = await self.editing.entries(session.id)
        await self.editing.toggle_exclude(
            owner_id=7,
            session_id=session.id,
            entry_id=int(entries[0].entry.id),
            expected_revision=draft.revision,
        )
        draft = await self.editing.draft(session.id)
        assert draft is not None
        token = await self.editing.issue_confirm(
            owner_id=7,
            session_id=session.id,
            expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        first = await self.editing.confirm(
            owner_id=7,
            chat_id=42,
            token=token,
            destination="@channel",
            max_items=100,
            ask_timeout_seconds=60,
        )
        self.assertTrue(first.jobs)
        job_ids = {accepted.job.id for accepted in first.jobs}
        self.assertEqual(len(job_ids), 1)

        # The same token can no longer inspect (single-use), but the durable
        # submission keeps a repeat request idempotent.
        submission = await self.repo.get_submission(session.id)
        self.assertIsNotNone(submission)
        self.assertEqual(submission.state, "created")
        self.assertEqual(set(submission.job_ids), job_ids)
        self.assertIsNone(await self.intake.open_collection(owner_id=7, chat_id=42))
        draft = await self.editing.draft(session.id)
        assert draft is not None
        self.assertEqual(draft.state, DraftState.SUBMITTED)

    async def test_confirm_rejects_stale_revision(self) -> None:
        session, draft = await self._session_with(2)
        token = await self.editing.issue_confirm(
            owner_id=7,
            session_id=session.id,
            expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
        )
        entries = await self.editing.entries(session.id)
        await self.editing.toggle_exclude(
            owner_id=7,
            session_id=session.id,
            entry_id=int(entries[0].entry.id),
            expected_revision=draft.revision,
        )
        with self.assertRaises(DraftRevisionConflict):
            await self.editing.confirm(
                owner_id=7,
                chat_id=42,
                token=token,
                destination="@channel",
                max_items=100,
                ask_timeout_seconds=60,
            )

    async def test_save_activate_and_discard_saved_draft(self) -> None:
        session, draft = await self._session_with(1)
        saved = await self.editing.save(
            owner_id=7, session_id=session.id, expected_revision=draft.revision
        )
        self.assertEqual(saved.state, DraftState.SAVED)
        self.assertFalse(saved.active)
        self.assertIsNone(await self.intake.open_collection(owner_id=7, chat_id=42))

        activated = await self.editing.activate(owner_id=7, session_id=session.id)
        self.assertTrue(activated.active)
        self.assertEqual(activated.state, DraftState.COLLECTING)
        self.assertIsNotNone(await self.intake.open_collection(owner_id=7, chat_id=42))

        await self.editing.discard(owner_id=7, session_id=session.id)
        self.assertIsNone(await self.intake.open_collection(owner_id=7, chat_id=42))
        draft = await self.editing.draft(session.id)
        assert draft is not None
        self.assertEqual(draft.state, DraftState.DISCARDED)

    async def test_new_collection_replaces_only_active_draft(self) -> None:
        first = await self.intake.begin_collection(owner_id=7, chat_id=42)
        draft = await self.editing.draft(first.id)
        assert draft is not None
        await self.editing.save(owner_id=7, session_id=first.id, expected_revision=draft.revision)
        second = await self.intake.begin_collection(owner_id=7, chat_id=42)
        self.assertNotEqual(second.id, first.id)
        self.assertIsNotNone(await self.editing.draft(second.id))
        first_draft = await self.editing.draft(first.id)
        assert first_draft is not None
        self.assertFalse(first_draft.active)


if __name__ == "__main__":
    unittest.main()
