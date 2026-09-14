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
from tgvio.application.suggestions import SuggestionService
from tgvio.domain.job import MediaKind
from tgvio.domain.suggestion import SuggestionKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


def _photo(message_id: int, name: str, *, sha: str | None = None) -> IncomingMedia:
    return IncomingMedia(
        kind=MediaKind.PHOTO,
        source=f"telegram:42:{message_id}",
        source_chat_id=42,
        source_message_id=message_id,
        size_bytes=100,
        name=name,
        metadata={"sha256": sha} if sha else {},
    )


class SuggestionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.intake = IntakeService(self.repo)
        self.editing = CollectionEditingService(
            self.repo, self.intake, OperationTokenService(self.repo)
        )
        self.service = SuggestionService(self.repo, self.editing)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _session(self, media: list[IncomingMedia]):
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        await self.intake.add_collection_media(session, media)
        draft = await self.editing.draft(session.id)
        assert draft is not None
        return session, draft

    async def test_analyze_suggests_cover_order_and_duplicates(self) -> None:
        session, draft = await self._session(
            [
                _photo(1, "clip-10.jpg", sha="dup"),
                _photo(2, "clip-2.jpg"),
                _photo(3, "clip-1.jpg", sha="dup"),
            ]
        )
        suggestions = await self.service.analyze(session.id)
        kinds = {item.kind for item in suggestions}
        self.assertIn(SuggestionKind.COVER, kinds)
        self.assertIn(SuggestionKind.ORDER, kinds)
        self.assertIn(SuggestionKind.DUPLICATE, kinds)
        duplicate = next(item for item in suggestions if item.kind == SuggestionKind.DUPLICATE)
        self.assertFalse(duplicate.uncertain)

    async def test_uncertain_duplicate_uses_name_and_size_only(self) -> None:
        session, _ = await self._session(
            [_photo(1, "same.jpg"), _photo(2, "same.jpg")]
        )
        suggestions = await self.service.analyze(session.id)
        duplicate = next(item for item in suggestions if item.kind == SuggestionKind.DUPLICATE)
        self.assertTrue(duplicate.uncertain)

    async def test_apply_order_and_undo_restores_positions(self) -> None:
        session, draft = await self._session(
            [_photo(1, "b.jpg"), _photo(2, "a.jpg")]
        )
        before = await self.repo.overlay_snapshot(session.id)
        applied = await self.service.apply_order(
            owner_id=7, session_id=session.id, expected_revision=draft.revision
        )
        self.assertEqual(applied.revision, draft.revision + 1)
        entries = await self.editing.entries(session.id)
        names = [str(item.entry.payload.get("name")) for item in entries]
        self.assertEqual(names, ["a.jpg", "b.jpg"])

        restored = await self.service.undo(
            owner_id=7, session_id=session.id, expected_revision=applied.revision
        )
        after = await self.repo.overlay_snapshot(session.id)
        self.assertEqual(after["positions"], before["positions"])
        self.assertEqual(
            [str(item.entry.payload.get("name")) for item in await self.editing.entries(session.id)],
            ["b.jpg", "a.jpg"],
        )
        self.assertEqual(restored.revision, applied.revision + 1)

    async def test_undo_rejects_stale_revision(self) -> None:
        session, draft = await self._session([_photo(1, "b.jpg"), _photo(2, "a.jpg")])
        applied = await self.service.apply_order(
            owner_id=7, session_id=session.id, expected_revision=draft.revision
        )
        entries = await self.editing.entries(session.id)
        await self.editing.toggle_exclude(
            owner_id=7,
            session_id=session.id,
            entry_id=int(entries[0].entry.id),
            expected_revision=applied.revision,
        )
        with self.assertRaises(DraftRevisionConflict):
            await self.service.undo(
                owner_id=7, session_id=session.id, expected_revision=applied.revision
            )

    async def test_apply_cover_records_application(self) -> None:
        session, draft = await self._session([_photo(1, "a.jpg"), _photo(2, "b.jpg")])
        entries = await self.editing.entries(session.id)
        target = int(entries[1].entry.id)
        applied = await self.service.apply_cover(
            owner_id=7,
            session_id=session.id,
            entry_id=target,
            expected_revision=draft.revision,
        )
        self.assertEqual(applied.cover_entry_id, target)
        application = await self.repo.get_active_suggestion_application(session.id)
        self.assertIsNotNone(application)
        self.assertEqual(application.revision_applied, applied.revision)


if __name__ == "__main__":
    unittest.main()
