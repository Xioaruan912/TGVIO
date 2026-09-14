from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import secrets
import shutil
import time

from tgvio.application.collection_editing import DraftRevisionConflict, DraftUnavailableError
from tgvio.application.ports import JobRepository
from tgvio.domain.collection_editing import CollectionDraft, DraftState
from tgvio.domain.intake import CollectionEntryKind, SpoilerMode
from tgvio.domain.job import MediaItem, MediaKind
from tgvio.domain.preview import PreviewRequest, PreviewState


class PreviewUnavailableError(RuntimeError):
    pass


_CLEANUP_PAGE = 200
_DIR_POLL_SECONDS = 0.2


class PreviewService:
    """Bounded, owner-scoped effect preview.

    Downloads at most one cover source into an isolated cache directory with an
    enforced byte budget, renders a representative frame, sends it once to the
    owner's private chat and always removes the cache. It never creates a Job,
    never touches the publish pipeline, never contacts Archive and never uses or
    cleans formal job caches.
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
        max_active_owners: int | None = None,
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
        self._max_active_owners = max(1, int(max_active_owners or max_concurrency))
        self._active_owners: set[int] = set()
        self._admission = asyncio.Lock()
        self._now = now or time.time

    # ------------------------------------------------------------- lifecycle
    async def mark_interrupted(self) -> int:
        count = await self._repository.mark_running_previews_interrupted()
        await self.cleanup_cache()
        return count

    async def cleanup_cache(self) -> int:
        """Paginated, bounded cleanup of preview-owned directories only."""
        removed = 0
        offset = 0
        while True:
            ids = await self._repository.list_preview_request_ids(
                limit=_CLEANUP_PAGE, offset=offset
            )
            if not ids:
                break
            for request_id in ids:
                path = self._safe_dir(request_id)
                if path is None:
                    continue
                try:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                        removed += 1
                except OSError:
                    continue
            offset += len(ids)
            if len(ids) < _CLEANUP_PAGE:
                break
        return removed

    def _safe_dir(self, request_id: str) -> Path | None:
        if any(path.is_symlink() for path in (self._cache_root, *self._cache_root.parents)):
            return None
        value = str(request_id or "")
        if not value or not all(c.isalnum() or c in "-_" for c in value):
            return None
        candidate = self._cache_root / f"preview-{value}"
        try:
            root = self._cache_root.resolve()
            resolved = candidate.resolve()
            if resolved == root or root not in resolved.parents:
                return None
        except OSError:
            return None
        if candidate.is_symlink():
            return None
        return candidate

    @staticmethod
    def _dir_size(path: Path) -> int:
        total = 0
        try:
            for child in path.rglob("*"):
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
        except OSError:
            return total
        return total

    # -------------------------------------------------------------- admission
    async def preview(
        self,
        *,
        owner_id: int,
        chat_id: int,
        session_id: str,
        expected_revision: int,
    ) -> PreviewRequest:
        owner = int(owner_id)
        draft = await self._repository.get_draft(session_id)
        if not self._draft_usable(draft, owner, expected_revision):
            raise DraftRevisionConflict("draft revision changed")
        item = await self._cover_item(draft)  # type: ignore[arg-type]
        if item is None:
            raise PreviewUnavailableError("合集里还没有可用于预览的媒体")

        async with self._admission:
            if owner in self._active_owners:
                raise PreviewUnavailableError("已有预览正在生成，请稍后重试")
            if len(self._active_owners) >= self._max_active_owners:
                raise PreviewUnavailableError("预览队列已满，请稍后重试")
            self._active_owners.add(owner)
        acquired = False
        try:
            request = await self._repository.create_preview_request(
                PreviewRequest(
                    id=secrets.token_urlsafe(12),
                    session_id=session_id,
                    owner_id=owner,
                    chat_id=int(chat_id),
                    revision=int(expected_revision),
                    state=PreviewState.PENDING,
                    cover_entry_id=draft.cover_entry_id,  # type: ignore[union-attr]
                    expires_at=int(self._now()) + max(60, int(self._timeout) * 2),
                )
            )
            if int(item.size_bytes or 0) > self._max_source_bytes:
                await self._fail(request.id, "source_too_large")
                raise PreviewUnavailableError("封面来源超过预览大小预算")

            deadline = self._now() + self._timeout * 2
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=max(1.0, deadline - self._now())
                )
                acquired = True
            except asyncio.TimeoutError:
                await self._fail(request.id, "queue_timeout")
                raise PreviewUnavailableError("预览排队超时，请稍后重试")

            remaining = deadline - self._now()
            current = await self._repository.get_preview_request(request.id)
            if (
                remaining <= 0
                or current is None
                or current.expires_at <= int(self._now())
            ):
                await self._fail(request.id, "expired")
                raise PreviewUnavailableError("预览请求已过期，请重新生成")
            refreshed = await self._repository.get_draft(session_id)
            if not self._draft_usable(refreshed, owner, expected_revision):
                await self._fail(request.id, "stale")
                raise DraftRevisionConflict("draft revision changed")

            try:
                return await asyncio.wait_for(
                    self._run(request, item, int(chat_id)), timeout=max(1.0, remaining)
                )
            except asyncio.TimeoutError:
                await self._fail(request.id, "timeout")
                updated = await self._repository.get_preview_request(request.id)
                return updated or request
        finally:
            if acquired:
                self._semaphore.release()
            async with self._admission:
                self._active_owners.discard(owner)

    @staticmethod
    def _draft_usable(draft: CollectionDraft | None, owner: int, revision: int) -> bool:
        return (
            draft is not None
            and int(draft.owner_id) == int(owner)
            and int(draft.revision) == int(revision)
            and draft.state in {DraftState.COLLECTING, DraftState.PREVIEW, DraftState.SAVED}
        )

    async def _fail(self, request_id: str, code: str) -> None:
        await self._repository.update_preview_request(
            request_id, state=PreviewState.FAILED, error_code=code
        )

    # ------------------------------------------------------------------- work
    async def _run(self, request: PreviewRequest, item: MediaItem, chat_id: int) -> PreviewRequest:
        cache_dir = self._safe_dir(request.id)
        if cache_dir is None:
            await self._fail(request.id, "unsafe_path")
            raise PreviewUnavailableError("unsafe preview path")
        cache_dir.mkdir(parents=True, exist_ok=True)
        await self._repository.update_preview_request(
            request.id, state=PreviewState.RUNNING, cache_dir=str(cache_dir)
        )
        try:
            await self._generate_and_send(request, item, cache_dir, chat_id)
            await self._repository.update_preview_request(
                request.id, state=PreviewState.SUCCEEDED
            )
        except asyncio.CancelledError:
            await self._fail(request.id, "cancelled")
            raise
        except DraftRevisionConflict:
            await self._fail(request.id, "stale")
        except PreviewUnavailableError as exc:
            await self._fail(request.id, str(exc)[:80] or "unavailable")
            raise
        except Exception as exc:  # noqa: BLE001 - terminal, no auto-resend
            await self._fail(request.id, type(exc).__name__)
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)
        updated = await self._repository.get_preview_request(request.id)
        return updated or request

    async def _generate_and_send(self, request, item, cache_dir, chat_id):
        _, cover = await self._generate(item, cache_dir)
        # Re-check owner/revision/state/TTL immediately before sending.
        draft = await self._repository.get_draft(request.session_id)
        if (
            not self._draft_usable(draft, request.owner_id, request.revision)
            or request.expires_at <= int(self._now())
        ):
            raise DraftRevisionConflict("preview is stale")
        preference = await self._repository.get_user_preference(request.owner_id)
        spoiler = bool(item.spoiler) or preference.spoiler_mode in {
            SpoilerMode.ASK,
            SpoilerMode.ALWAYS_SPOILER,
        }
        # A single send; an uncertain response is never auto-retried.
        await self._sender.send_preview(
            int(chat_id), cover, spoiler=spoiler,
            caption="🖼 效果预览（示意，非像素级最终结果）\n确认前不会发布，也不会归档。",
        )

    async def _generate(self, item: MediaItem, cache_dir: Path):
        downloaded = await self._download_bounded(item, cache_dir)
        local_path = getattr(downloaded, "local_path", None)
        source = Path(local_path) if local_path else None
        if source is None or not source.is_file():
            raise PreviewUnavailableError("download produced no file")
        if source.is_symlink() or cache_dir.resolve() not in source.resolve().parents:
            raise PreviewUnavailableError("unsafe preview source")
        if source.stat().st_size > self._max_source_bytes:
            raise PreviewUnavailableError("source exceeds budget")
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

    async def _download_bounded(self, item: MediaItem, cache_dir: Path):
        if int(item.size_bytes or 0) > self._max_source_bytes:
            raise PreviewUnavailableError("source_too_large")
        download = asyncio.create_task(self._downloader.download_bounded(
            item, cache_dir, max_bytes=self._max_source_bytes,
        ))
        try:
            while not download.done():
                await asyncio.sleep(_DIR_POLL_SECONDS)
                if self._dir_size(cache_dir) > self._max_source_bytes:
                    download.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await download
                    raise PreviewUnavailableError("source exceeds budget")
            return await download
        finally:
            if not download.done():
                download.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await download

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
