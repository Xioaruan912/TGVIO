from __future__ import annotations

import asyncio
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
    async def download_bounded(self, item, target_dir, *, max_bytes):
        return await self.download(item, target_dir)

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
    async def test_unsafe_directory_never_falls_back_to_unchecked_path(self):
        session, draft = await self._session([IncomingMedia(
            kind=MediaKind.PHOTO, source="telegram:42:99", size_bytes=10,
            source_chat_id=42, source_message_id=99,
        )])
        self.service._safe_dir = lambda request_id: None
        with self.assertRaises(PreviewUnavailableError):
            await self.service.preview(owner_id=7, chat_id=42, session_id=session.id,
                                       expected_revision=draft.revision)
        self.assertEqual(self.downloader.calls, 0)
        self.assertEqual(self.sender.calls, [])

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



    async def test_actual_download_over_budget_is_cancelled(self) -> None:
        class _Flood(_Downloader):
            def __init__(self) -> None:
                self.calls = 0

            async def download(self, item, target_dir, progress_callback=None):
                self.calls += 1
                target_dir.mkdir(parents=True, exist_ok=True)
                path = target_dir / "flood.bin"
                path.write_bytes(b"x" * (self.limit * 4))
                await asyncio.sleep(1.0)
                return replace(item, local_path=str(path))

        flood = _Flood()
        flood.limit = 1024
        service = PreviewService(
            self.repo,
            flood,
            self.cover,
            self.sender,
            cache_root=self.root / "downloads",
            max_source_bytes=1024,
            timeout_seconds=30,
        )
        session, draft = await self._session(
            [
                IncomingMedia(
                    kind=MediaKind.PHOTO,
                    source="telegram:42:9",
                    source_chat_id=42,
                    source_message_id=9,
                    size_bytes=10,
                )
            ]
        )
        with self.assertRaises(PreviewUnavailableError):
            await service.preview(
                owner_id=7, chat_id=42, session_id=session.id, expected_revision=draft.revision
            )
        self.assertEqual(len(self.sender.calls), 0)

    async def test_duplicate_preview_for_same_owner_is_rejected(self) -> None:
        gate = asyncio.Event()

        class _Blocking(_Downloader):
            async def download(self, item, target_dir, progress_callback=None):
                target_dir.mkdir(parents=True, exist_ok=True)
                path = target_dir / "source.bin"
                path.write_bytes(b"0123456789")
                await gate.wait()
                return replace(item, local_path=str(path))

        service = PreviewService(
            self.repo,
            _Blocking(),
            self.cover,
            self.sender,
            cache_root=self.root / "downloads",
            max_source_bytes=1024 * 1024,
            timeout_seconds=30,
            max_concurrency=2,
        )
        session, draft = await self._session(
            [
                IncomingMedia(
                    kind=MediaKind.PHOTO,
                    source="telegram:42:11",
                    source_chat_id=42,
                    source_message_id=11,
                    size_bytes=10,
                )
            ]
        )
        first = asyncio.create_task(
            service.preview(
                owner_id=7, chat_id=42, session_id=session.id, expected_revision=draft.revision
            )
        )
        await asyncio.sleep(0.1)
        with self.assertRaises(PreviewUnavailableError):
            await service.preview(
                owner_id=7, chat_id=42, session_id=session.id, expected_revision=draft.revision
            )
        gate.set()
        await first

    async def test_cleanup_ignores_path_escape_and_symlinks(self) -> None:
        outside = self.root / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "keep.txt").write_text("keep")
        root = self.root / "downloads"
        root.mkdir(parents=True, exist_ok=True)
        link = root / "preview-evil"
        link.symlink_to(outside, target_is_directory=True)
        safe_dir = root / "preview-good"
        safe_dir.mkdir(parents=True, exist_ok=True)
        (safe_dir / "tmp.bin").write_bytes(b"x")

        async with self.repo._write_transaction() as conn:
            for rid in ("evil", "good"):
                await conn.execute(
                    "INSERT INTO preview_requests(id, session_id, owner_id, revision, state, expires_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (rid, "s", 7, 1, "failed", 0),
                )
        service = PreviewService(
            self.repo,
            self.downloader,
            self.cover,
            self.sender,
            cache_root=root,
            max_source_bytes=1024,
            timeout_seconds=30,
        )
        await service.cleanup_cache()
        self.assertFalse(safe_dir.exists())
        # Symlinked/branching path must not traverse or delete outside the root.
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue(link.is_symlink())
        self.assertIsNone(service._safe_dir("../evil"))
        self.assertIsNone(service._safe_dir("bad/name"))

    async def test_mark_interrupted_removes_recorded_preview_dir(self) -> None:
        root = self.root / "downloads"
        orphan = root / "preview-orphan"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "part.bin").write_bytes(b"x")
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                "INSERT INTO preview_requests(id, session_id, owner_id, revision, state, expires_at) "
                "VALUES('orphan','s',7,1,'running',0)"
            )
        count = await self.service.mark_interrupted()
        self.assertEqual(count, 1)
        self.assertFalse(orphan.exists())

if __name__ == "__main__":
    unittest.main()
