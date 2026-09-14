from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.collection_editing import (
    CollectionEditingService,
    DraftRevisionConflict,
)
from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.operation_tokens import OperationTokenService
from tgvio.application.previews import PreviewService, PreviewUnavailableError
from tgvio.domain.job import MediaKind
from tgvio.domain.preview import PreviewState
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class _Downloader:
    def __init__(self) -> None:
        self.calls = 0

    async def download(self, item, target_dir, progress_callback=None):
        self.calls += 1
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "source.bin"
        path.write_bytes(b"0123456789")
        return replace(item, local_path=str(path))


class _CoverFactory:
    def __init__(self) -> None:
        self.calls = 0

    async def make_video_cover(self, source, target_dir, *, item_index, duration_seconds, max_width):
        self.calls += 1
        out = target_dir / "cover-0.jpg"
        out.write_bytes(b"jpg")
        return out


class _Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, bool]] = []

    async def send_preview(self, chat_id, path, *, spoiler, caption):
        self.calls.append((int(chat_id), str(path), bool(spoiler)))


class PreviewServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_spoiler_and_changed_revision_are_enforced_before_send(self) -> None:
        from tgvio.domain.intake import SpoilerMode
        session, draft = await self._session([IncomingMedia(
            kind=MediaKind.PHOTO, source="telegram:42:10", size_bytes=10,
            source_chat_id=42, source_message_id=10,
        )])
        await self.intake.set_spoiler_mode(7, SpoilerMode.ALWAYS_SPOILER)
        await self.service.preview(owner_id=7, chat_id=42, session_id=session.id,
                                   expected_revision=draft.revision)
        self.assertTrue(self.sender.calls[0][2])
        download = self.downloader.download
        async def changed(item, target):
            await self.repo.set_draft_caption(session.id, text="changed",
                                              expected_revision=draft.revision)
            return await download(item, target)
        self.downloader.download = changed
        result = await self.service.preview(owner_id=7, chat_id=42, session_id=session.id,
                                            expected_revision=draft.revision)
        self.assertEqual(result.state, PreviewState.FAILED)
        self.assertEqual(len(self.sender.calls), 1)

    async def test_sender_timeout_is_bounded_and_cleans_cache(self) -> None:
        import asyncio
        session, draft = await self._session([IncomingMedia(
            kind=MediaKind.PHOTO, source="telegram:42:11", size_bytes=10,
            source_chat_id=42, source_message_id=11,
        )])
        async def hung(*args, **kwargs):
            await asyncio.Event().wait()
        self.sender.send_preview = hung
        self.service._timeout = 0.02
        result = await self.service.preview(owner_id=7, chat_id=42, session_id=session.id,
                                            expected_revision=draft.revision)
        self.assertEqual(result.error_code, "timeout")
        self.assertFalse((self.root / "downloads" / f"preview-{result.id}").exists())

    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = SQLiteJobRepository(self.root / "state.sqlite3")
        await self.repo.open()
        self.intake = IntakeService(self.repo)
        self.editing = CollectionEditingService(
            self.repo, self.intake, OperationTokenService(self.repo)
        )
        self.downloader = _Downloader()
        self.cover = _CoverFactory()
        self.sender = _Sender()
        self.service = PreviewService(
            self.repo,
            self.downloader,
            self.cover,
            self.sender,
            cache_root=self.root / "downloads",
            max_source_bytes=1024 * 1024,
            timeout_seconds=30,
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _session(self, media: list[IncomingMedia]):
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        await self.intake.add_collection_media(session, media)
        draft = await self.editing.draft(session.id)
        assert draft is not None
        return session, draft

    async def test_photo_preview_downloads_once_sends_and_cleans_cache(self) -> None:
        session, draft = await self._session(
            [
                IncomingMedia(
                    kind=MediaKind.PHOTO,
                    source="telegram:42:1",
                    source_chat_id=42,
                    source_message_id=1,
                    size_bytes=10,
                )
            ]
        )
        request = await self.service.preview(
            owner_id=7, chat_id=42, session_id=session.id, expected_revision=draft.revision
        )
        self.assertEqual(request.state, PreviewState.SUCCEEDED)
        self.assertEqual(self.downloader.calls, 1)
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.sender.calls[0][0], 42)
        # The bounded preview cache is always removed.
        cache_dir = self.root / "downloads" / f"preview-{request.id}"
        self.assertFalse(cache_dir.exists())

    async def test_video_preview_generates_cover_frame(self) -> None:
        session, draft = await self._session(
            [
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:2",
                    source_chat_id=42,
                    source_message_id=2,
                    size_bytes=10,
                    metadata={"duration_seconds": 12.0},
                )
            ]
        )
        request = await self.service.preview(
            owner_id=7, chat_id=42, session_id=session.id, expected_revision=draft.revision
        )
        self.assertEqual(request.state, PreviewState.SUCCEEDED)
        self.assertEqual(self.cover.calls, 1)

    async def test_source_over_budget_fails_without_download(self) -> None:
        session, draft = await self._session(
            [
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:42:3",
                    source_chat_id=42,
                    source_message_id=3,
                    size_bytes=10 * 1024 * 1024,
                )
            ]
        )
        with self.assertRaises(PreviewUnavailableError):
            await self.service.preview(
                owner_id=7, chat_id=42, session_id=session.id, expected_revision=draft.revision
            )
        self.assertEqual(self.downloader.calls, 0)

    async def test_stale_revision_is_rejected(self) -> None:
        session, draft = await self._session(
            [
                IncomingMedia(
                    kind=MediaKind.PHOTO,
                    source="telegram:42:4",
                    source_chat_id=42,
                    source_message_id=4,
                    size_bytes=10,
                )
            ]
        )
        with self.assertRaises(DraftRevisionConflict):
            await self.service.preview(
                owner_id=7,
                chat_id=42,
                session_id=session.id,
                expected_revision=draft.revision + 5,
            )

    async def test_mark_interrupted_fails_pending_requests(self) -> None:
        session, draft = await self._session(
            [
                IncomingMedia(
                    kind=MediaKind.PHOTO,
                    source="telegram:42:5",
                    source_chat_id=42,
                    source_message_id=5,
                    size_bytes=10,
                )
            ]
        )
        from tgvio.domain.preview import PreviewRequest

        await self.repo.create_preview_request(
            PreviewRequest(
                id="p1",
                session_id=session.id,
                owner_id=7,
                chat_id=42,
                revision=draft.revision,
                state=PreviewState.PENDING,
                expires_at=0,
            )
        )
        self.assertEqual(await self.service.mark_interrupted(), 1)
        request = await self.repo.get_preview_request("p1")
        assert request is not None
        self.assertEqual(request.state, PreviewState.FAILED)


if __name__ == "__main__":
    unittest.main()
