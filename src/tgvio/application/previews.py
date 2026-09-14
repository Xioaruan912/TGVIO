from __future__ import annotations

import asyncio
from pathlib import Path
import secrets
import shutil
import time

from tgvio.application.collection_editing import DraftRevisionConflict, DraftUnavailableError
from tgvio.application.ports import JobRepository
from tgvio.domain.collection_editing import CollectionDraft
from tgvio.domain.intake import CollectionEntryKind
from tgvio.domain.job import MediaItem, MediaKind
from tgvio.domain.preview import PreviewRequest, PreviewState


class PreviewUnavailableError(RuntimeError):
    pass


class PreviewService:
    """Bounded, owner-scoped effect preview.

    Downloads at most one cover source into an isolated cache directory, renders a
    representative frame, sends it to the owner's private chat, and always removes
    the cache. It never creates a Job, never touches the publish pipeline and never
    contacts Archive.
    """

    def __init__(
        self,
        repository: JobRepository,
        downloader,
        cover_factory,
        sender,
        *,
        cache_root: Path,
        max_source_bytes: int,
        timeout_seconds: float,
        cover_width: int = 1280,
        max_concurrency: int = 2,
        now=None,
    ) -> None:
        self._repository = repository
        self._downloader = downloader
        self._cover_factory = cover_factory
        self._sender = sender
        self._cache_root = Path(cache_root)
        self._max_source_bytes = max(1, int(max_source_bytes))
        self._timeout = max(5.0, float(timeout_seconds))
        self._cover_width = max(128, int(cover_width))
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._owner_locks: dict[int, asyncio.Lock] = {}
        self._now = now or time.time

    async def mark_interrupted(self) -> int:
        return await self._repository.mark_running_previews_interrupted()

    def _owner_lock(self, owner_id: int) -> asyncio.Lock:
        lock = self._owner_locks.get(int(owner_id))
        if lock is None:
            lock = asyncio.Lock()
            self._owner_locks[int(owner_id)] = lock
        return lock

    async def preview(
        self,
        *,
        owner_id: int,
        chat_id: int,
        session_id: str,
        expected_revision: int,
    ) -> PreviewRequest:
        draft = await self._repository.get_draft(session_id)
        if (
            draft is None
            or int(draft.owner_id) != int(owner_id)
            or int(draft.revision) != int(expected_revision)
        ):
            raise DraftRevisionConflict("draft revision changed")
        item = await self._cover_item(draft)
        if item is None:
            raise PreviewUnavailableError("合集里还没有可用于预览的媒体")
        request = await self._repository.create_preview_request(
            PreviewRequest(
                id=secrets.token_urlsafe(12),
                session_id=session_id,
                owner_id=int(owner_id),
                chat_id=int(chat_id),
                revision=int(expected_revision),
                state=PreviewState.PENDING,
                cover_entry_id=draft.cover_entry_id,
                expires_at=int(self._now()) + max(60, int(self._timeout) * 2),
            )
        )
        if int(item.size_bytes or 0) > self._max_source_bytes:
            await self._repository.update_preview_request(
                request.id, state=PreviewState.FAILED, error_code="source_too_large"
            )
            raise PreviewUnavailableError("封面来源超过预览大小预算")
        async with self._owner_lock(int(owner_id)):
            async with self._semaphore:
                return await self._run(request, item, chat_id)

    async def _run(self, request: PreviewRequest, item: MediaItem, chat_id: int) -> PreviewRequest:
        cache_dir = self._cache_root / f"preview-{request.id}"
        await self._repository.update_preview_request(
            request.id, state=PreviewState.RUNNING, cache_dir=str(cache_dir)
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            download, cover = await asyncio.wait_for(
                self._generate(item, cache_dir), timeout=self._timeout
            )
            spoiler = bool(item.spoiler)
            await self._sender.send_preview(
                int(chat_id),
                cover,
                spoiler=spoiler,
                caption="🖼 效果预览（示意，非像素级最终结果）\n确认前不会发布，也不会归档。",
            )
            await self._repository.update_preview_request(
                request.id, state=PreviewState.SUCCEEDED
            )
        except asyncio.TimeoutError:
            await self._repository.update_preview_request(
                request.id, state=PreviewState.FAILED, error_code="timeout"
            )
        except Exception as exc:
            await self._repository.update_preview_request(
                request.id, state=PreviewState.FAILED, error_code=type(exc).__name__
            )
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)
        updated = await self._repository.get_preview_request(request.id)
        return updated or request

    async def _generate(self, item: MediaItem, cache_dir: Path):
        downloaded = await self._downloader.download(item, cache_dir)
        local_path = getattr(downloaded, "local_path", None)
        source = Path(local_path) if local_path else None
        if source is None or not source.is_file():
            raise PreviewUnavailableError("download produced no file")
        if item.kind == MediaKind.VIDEO:
            duration = 0.0
            try:
                duration = float((item.metadata or {}).get("duration_seconds") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            cover = await self._cover_factory.make_video_cover(
                source,
                cache_dir,
                item_index=0,
                duration_seconds=duration or None,
                max_width=self._cover_width,
            )
        else:
            cover = source
        return downloaded, cover

    async def _cover_item(self, draft: CollectionDraft) -> MediaItem | None:
        entries = await self._repository.list_draft_entries(draft.session_id)
        visible = [
            entry
            for entry in entries
            if entry.entry.kind == CollectionEntryKind.MEDIA
            and not entry.excluded
            and entry.entry.id is not None
        ]
        if not visible:
            return None
        chosen = None
        if draft.cover_entry_id is not None:
            chosen = next(
                (entry for entry in visible if int(entry.entry.id) == int(draft.cover_entry_id)),
                None,
            )
        if chosen is None:
            photos = [e for e in visible if str(e.entry.payload.get("kind")) == "photo"]
            chosen = photos[0] if photos else visible[0]
        payload = chosen.entry.payload
        return MediaItem(
            index=0,
            kind=MediaKind(str(payload.get("kind", "document"))),
            source=str(payload.get("source", "")),
            caption=str(payload.get("caption", "") or ""),
            size_bytes=int(payload.get("size_bytes", 0) or 0),
            name=payload.get("name"),
            spoiler=bool(payload.get("spoiler", False)),
            source_chat_id=payload.get("source_chat_id"),
            source_message_id=payload.get("source_message_id"),
            metadata=dict(payload.get("metadata", {}) or {}),
        )
