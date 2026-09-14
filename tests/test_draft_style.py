from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.collection_editing import CollectionEditingService
from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.operation_tokens import OperationTokenService
from tgvio.application.publish_styles import PublishStyleService
from tgvio.domain.intake import SpoilerMode
from tgvio.domain.job import MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


def _media(message_id: int) -> IncomingMedia:
    return IncomingMedia(
        kind=MediaKind.PHOTO,
        source=f"telegram:42:{message_id}",
        source_chat_id=42,
        source_message_id=message_id,
        size_bytes=10,
        name=f"p{message_id}.jpg",
    )


class DraftStyleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.intake = IntakeService(self.repo)
        self.tokens = OperationTokenService(self.repo)
        self.editing = CollectionEditingService(self.repo, self.intake, self.tokens)
        self.styles = PublishStyleService(self.repo)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_draft_override_takes_priority_without_changing_owner_default(self) -> None:
        await self.styles.set_named(7, "minimal")
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        await self.intake.add_collection_media(session, [_media(1)])
        draft = await self.editing.draft(session.id)
        assert draft is not None

        await self.editing.set_draft_style(
            owner_id=7,
            session_id=session.id,
            style_json=json.dumps({"style": "photo_text"}),
            expected_revision=draft.revision,
        )
        effective = await self.editing.effective_style(7, session.id)
        self.assertTrue(effective["cover_mode"])
        self.assertTrue(effective["forward_caption"])

        # Owner default is untouched.
        owner = await self.repo.get_user_preference(7)
        self.assertIn("minimal", owner.style_json or "")
        self.assertFalse((await self.styles.current(7))["forward_caption"])

    async def test_confirm_freezes_draft_override(self) -> None:
        await self.styles.set_named(7, "photo_text")
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        await self.intake.add_collection_media(session, [_media(2)])
        draft = await self.editing.draft(session.id)
        assert draft is not None
        await self.editing.set_draft_style(
            owner_id=7,
            session_id=session.id,
            style_json=json.dumps({"cover_mode": False, "forward_caption": False}),
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
        result = await self.editing.confirm(
            owner_id=7, chat_id=42, token=token, destination="@channel",
            max_items=100, ask_timeout_seconds=60,
        )
        job = result.jobs[0].job
        self.assertFalse(job.policy["publish_style"]["cover_mode"])
        # Frozen task style must not leak into the owner default.
        self.assertTrue((await self.styles.current(7))["cover_mode"])

    async def test_clear_draft_style_falls_back_to_owner_default(self) -> None:
        await self.styles.set_named(7, "minimal")
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        await self.intake.add_collection_media(session, [_media(3)])
        draft = await self.editing.draft(session.id)
        assert draft is not None
        await self.editing.set_draft_style(
            owner_id=7,
            session_id=session.id,
            style_json=json.dumps({"style": "photo_text"}),
            expected_revision=draft.revision,
        )
        draft = await self.editing.draft(session.id)
        assert draft is not None
        await self.editing.set_draft_style(
            owner_id=7,
            session_id=session.id,
            style_json=None,
            expected_revision=draft.revision,
        )
        effective = await self.editing.effective_style(7, session.id)
        self.assertFalse(effective["cover_mode"])
        self.assertEqual(await self.editing.effective_style_name(7, session.id), "minimal")

    async def test_repost_creates_empty_draft_with_style_only(self) -> None:
        session = await self.intake.begin_collection(
            owner_id=7,
            chat_id=42,
            style_json=json.dumps({"cover_mode": False, "forward_caption": False}),
        )
        draft = await self.editing.draft(session.id)
        assert draft is not None
        self.assertEqual(json.loads(draft.style_json or "{}")["cover_mode"], False)
        self.assertEqual(await self.intake.collection_counts(session.id), (0, 0))
        # Reusing the same owner/chat returns the existing empty draft (no duplicates).
        again = await self.intake.begin_collection(owner_id=7, chat_id=42)
        self.assertEqual(again.id, session.id)
        owner = await self.repo.get_user_preference(7)
        self.assertIsNone(owner.style_json)


if __name__ == "__main__":
    unittest.main()
