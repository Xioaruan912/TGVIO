from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
import logging
from pathlib import PurePosixPath
import re
import time
from typing import Awaitable, Callable, Literal, Protocol

from tgvio_player.application.ports import PlayerCatalogRepository, WebDavWriteClient, WebDavWriteError
from tgvio_player.domain.storage_settings import safe_storage_relpath


_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_MIME_TO_EXTENSION = {
    "video/mp4": (".mp4", "video/mp4"),
    "video/quicktime": (".mov", "video/quicktime"),
    "video/x-matroska": (".mkv", "video/x-matroska"),
    "video/webm": (".webm", "video/webm"),
    "video/x-msvideo": (".avi", "video/x-msvideo"),
    "video/mpeg": (".mpeg", "video/mpeg"),
    "video/3gpp": (".3gp", "video/3gpp"),
    "video/ogg": (".ogv", "video/ogg"),
    "video/mp2t": (".ts", "video/mp2t"),
}
_CONTAINER_TO_MIME = {
    "mp4": "video/mp4", "mov": "video/quicktime", "mkv": "video/x-matroska",
    "webm": "video/webm", "avi": "video/x-msvideo", "mpeg": "video/mpeg",
    "3gp": "video/3gpp", "ogv": "video/ogg", "ts": "video/mp2t",
}
_LOG = logging.getLogger("tgvio_player.favorite_backup")


class FavoriteBackupError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"favorite backup failed: {category}")


class SourceMediaError(RuntimeError):
    def __init__(self, category: str, status_code: int | None = None) -> None:
        self.category = category
        self.status_code = status_code
        super().__init__(f"favorite source unavailable: {category}")


@dataclass(frozen=True, slots=True)
class FavoriteSyncStatus:
    media_id: str
    favorite: bool
    sync_status: Literal["pending", "synced", "failed"]
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SyncBatchResult:
    processed: int
    synced: int
    retried: int
    failed: int


class SnapshotExporter(Protocol):
    async def export_state(self) -> object: ...


SourceStreamProvider = Callable[
    [str], Awaitable[tuple[int, str | None, AsyncIterator[bytes]]]
]
SourceLocationProvider = Callable[[str], Awaitable[tuple[str, str] | None]]


class FavoriteBackupService:
    def __init__(
        self,
        repository: PlayerCatalogRepository,
        writer: WebDavWriteClient,
        source_stream: SourceStreamProvider,
        snapshot_exporter: SnapshotExporter,
        *,
        source_location: SourceLocationProvider | None = None,
        clock: Callable[[], float] = time.time,
        tombstone_retry_seconds: float = 60.0,
    ) -> None:
        self._repository = repository
        self._writer = writer
        self._source_stream = source_stream
        self._snapshot_exporter = snapshot_exporter
        self._source_location = source_location
        self._clock = clock
        self._tombstone_retry_seconds = max(0.0, float(tombstone_retry_seconds))
        self._tombstone_retry_after = 0.0

    def replace_writer(self, writer: WebDavWriteClient) -> WebDavWriteClient:
        previous = self._writer
        self._writer = writer
        return previous

    async def retry_failed(self) -> int:
        """Requeue failed jobs; delete jobs still require a durable tombstone."""
        return await self._repository.retry_failed_favorite_sync()

    async def favorite(self, media_id: str) -> FavoriteSyncStatus:
        self._require_media_id(media_id)
        details = await self._repository.active_media_details(media_id)
        if details is None or details.get("kind") != "video":
            raise FavoriteBackupError("media_not_found")
        await self._repository.set_global_favorite(media_id, True)
        location = await self._repository.get_favorite_location(media_id)
        if location is not None:
            relpath, size_bytes, _mime_type = location
            settings = await self._repository.get_storage_settings()
            try:
                remote = await self._writer.stat(self._remote_path(settings.player_root, relpath))
            except WebDavWriteError:
                remote = None
            if remote is not None and remote.size_bytes == size_bytes:
                return FavoriteSyncStatus(media_id, True, "synced")
        await self._repository.enqueue_favorite_sync(media_id, "upload")
        return FavoriteSyncStatus(media_id, True, "pending")

    async def unfavorite(self, media_id: str) -> FavoriteSyncStatus:
        self._require_media_id(media_id)
        was_favorite = await self._repository.is_global_favorite(media_id)
        location = await self._repository.get_favorite_location(media_id)
        jobs = await self._repository.list_pending_favorite_sync()
        delete_job = next(
            (job for job in jobs if job.media_id == media_id and job.operation == "delete"), None
        )
        if not was_favorite and location is None and delete_job is None:
            return FavoriteSyncStatus(media_id, False, "synced")
        await self._repository.set_global_favorite(media_id, False)
        await self._repository.enqueue_favorite_sync(media_id, "delete")
        # The remote state snapshot carrying this delete intent is written by the
        # sync worker before the delete is ever claimed (see
        # _persist_pending_delete_intents). Doing it here would put archive I/O on
        # the request path, so a slow or failing archive turned a removal that had
        # already been applied locally into an HTTP 502.
        return FavoriteSyncStatus(media_id, False, "pending")

    async def _persist_pending_delete_intents(self) -> None:
        """Make pending delete intents durable in the remote snapshot.

        A delete job is only claimable once `intent_persisted` is set, and the
        snapshot export is what sets it. Unfavorite used to call that export
        inline, which blocked the request on the archive backend. It now runs
        here, once per sync cycle; a failure leaves the job pending for the next
        attempt instead of failing a user request.
        """
        now = self._clock()
        if now < self._tombstone_retry_after:
            return
        pending = await self._repository.list_pending_favorite_sync()
        blocked = [
            job for job in pending if job.operation == "delete" and not job.intent_persisted
        ]
        if not blocked:
            return
        try:
            await self._snapshot_exporter.export_state()
            for job in blocked:
                await self._repository.mark_favorite_delete_intent(job.media_id)
        except Exception as exc:  # noqa: BLE001 - the next cycle retries
            self._tombstone_retry_after = now + self._tombstone_retry_seconds
            _LOG.warning(
                "Favorite delete intent could not be persisted; the delete stays withheld: "
                "media_count=%d operation=delete error=%s",
                len(blocked), type(exc).__name__,
            )

    async def sync_pending(self, limit: int = 2) -> SyncBatchResult:
        if limit < 1:
            return SyncBatchResult(0, 0, 0, 0)
        await self._persist_pending_delete_intents()
        jobs = await self._repository.claim_favorite_sync(limit=min(16, int(limit)))
        synced = retried = failed = 0
        for job in jobs:
            try:
                if job.operation == "upload":
                    await self._sync_upload(job.media_id)
                else:
                    await self._sync_delete(job.media_id)
            except SourceMediaError as exc:
                if exc.status_code == 404 or exc.category == "source_not_found":
                    await self._repository.finish_favorite_sync(job.job_id, "failed", "source_404")
                    failed += 1
                elif exc.status_code in {401, 403}:
                    await self._repository.finish_favorite_sync(job.job_id, "failed", "source_unauthorized")
                    failed += 1
                else:
                    await self._retry(job.job_id, job.attempts, "source_network_error")
                    retried += 1
            except WebDavWriteError as exc:
                _LOG.warning(
                    "Favorite WebDAV sync failed: media_id=%s operation=%s category=%s status=%s",
                    job.media_id, exc.operation, exc.category, exc.status_code,
                )
                if exc.category in {"unauthorized", "unsafe_redirect", "rejected", "move_unsupported", "not_found_or_conflict"}:
                    await self._repository.finish_favorite_sync(job.job_id, "failed", exc.category)
                    failed += 1
                else:
                    await self._retry(job.job_id, job.attempts, exc.category)
                    retried += 1
            except FavoriteBackupError as exc:
                if exc.category == "verification_mismatch":
                    await self._retry(job.job_id, job.attempts, exc.category)
                    retried += 1
                    continue
                await self._repository.finish_favorite_sync(job.job_id, "failed", exc.category)
                failed += 1
            except ValueError:
                await self._repository.finish_favorite_sync(job.job_id, "failed", "path_or_metadata_invalid")
                failed += 1
            except Exception:
                await self._retry(job.job_id, job.attempts, "unexpected_error")
                retried += 1
            else:
                await self._repository.finish_favorite_sync(job.job_id, "synced", None)
                synced += 1
        return SyncBatchResult(len(jobs), synced, retried, failed)

    async def _sync_upload(self, media_id: str) -> None:
        if not await self._repository.is_global_favorite(media_id):
            return
        details = await self._repository.active_media_details(media_id)
        if details is None or details.get("kind") != "video":
            raise FavoriteBackupError("media_not_found")
        size_bytes = int(details["size_bytes"])
        if size_bytes < 0:
            raise FavoriteBackupError("invalid_media_size")
        mime_type = self._safe_mime_type(details.get("mime_type"), details.get("container"))
        extension = _MIME_TO_EXTENSION[mime_type][0]
        settings = await self._repository.get_storage_settings()
        if self._source_location is not None:
            location = await self._source_location(media_id)
            copier = getattr(self._writer, "copy", None)
            if location is not None and copier is not None:
                source_path = safe_storage_relpath(f"{location[0]}/{location[1]}")
                source_name = PurePosixPath(source_path).name
                relative_path = safe_storage_relpath(
                    f"{settings.favorites_dir}/{media_id}/{source_name}"
                )
                remote_path = self._remote_path(settings.player_root, relative_path)
                await self._writer.ensure_directory(
                    self._remote_path(
                        settings.player_root, f"{settings.favorites_dir}/{media_id}",
                    )
                )
                try:
                    await copier(source_path, remote_path)
                except WebDavWriteError as exc:
                    if exc.category != "copy_unsupported":
                        raise
                else:
                    remote = await self._writer.stat(remote_path)
                    if remote is None or remote.size_bytes != size_bytes:
                        raise FavoriteBackupError("verification_mismatch")
                    await self._repository.save_favorite_location(
                        media_id, relative_path, size_bytes, mime_type,
                    )
                    await self._snapshot_exporter.export_state()
                    return
        source_size, source_mime, chunks = await self._source_stream(media_id)
        if source_size != size_bytes:
            raise FavoriteBackupError("source_size_mismatch")
        if source_mime:
            source_mime = source_mime.split(";", 1)[0].strip().lower()
            if source_mime in _MIME_TO_EXTENSION and source_mime != mime_type:
                raise FavoriteBackupError("source_mime_mismatch")
        relative_path = safe_storage_relpath(f"{settings.favorites_dir}/{media_id}{extension}")
        remote_path = self._remote_path(settings.player_root, relative_path)
        await self._writer.ensure_directory(self._remote_path(settings.player_root, settings.favorites_dir))
        receipt = await self._writer.put_stream(
            remote_path, chunks, size_bytes=size_bytes, content_type=mime_type,
        )
        if receipt.size_bytes != size_bytes:
            raise FavoriteBackupError("verification_mismatch")
        remote = await self._writer.stat(remote_path)
        if remote is None or remote.size_bytes != size_bytes:
            raise FavoriteBackupError("verification_mismatch")
        await self._repository.save_favorite_location(media_id, relative_path, size_bytes, mime_type)
        await self._snapshot_exporter.export_state()

    async def _sync_delete(self, media_id: str) -> None:
        location = await self._repository.get_favorite_location(media_id)
        if location is None:
            details = await self._repository.active_media_details(media_id)
            if details is None:
                raise FavoriteBackupError("favorite_location_missing")
            mime_type = self._safe_mime_type(details.get("mime_type"), details.get("container"))
            extension = _MIME_TO_EXTENSION[mime_type][0]
            settings = await self._repository.get_storage_settings()
            relative_path = f"{settings.favorites_dir}/{media_id}{extension}"
        else:
            relative_path = location[0]
            settings = await self._repository.get_storage_settings()
        remote_path = self._remote_path(settings.player_root, relative_path)
        receipt = await self._writer.delete(remote_path)
        if not receipt.deleted:
            raise FavoriteBackupError("delete_not_confirmed")
        await self._repository.delete_favorite_location(media_id)
        await self._snapshot_exporter.export_state()

    async def _retry(self, job_id: int, attempts: int, error_code: str) -> None:
        delay = min(3600, 5 * (2 ** min(max(0, attempts - 1), 10)))
        await self._repository.finish_favorite_sync(
            job_id, "retry", error_code, retry_at=int(self._clock()) + delay,
        )

    @staticmethod
    def _require_media_id(media_id: str) -> None:
        if not _MEDIA_ID_RE.fullmatch(media_id):
            raise FavoriteBackupError("invalid_media_id")

    @staticmethod
    def _safe_mime_type(mime_type: object, container: object) -> str:
        candidate = str(mime_type or "").split(";", 1)[0].strip().lower()
        if candidate not in _MIME_TO_EXTENSION:
            candidate = _CONTAINER_TO_MIME.get(str(container or "").strip().lower(), "")
        if candidate not in _MIME_TO_EXTENSION:
            raise FavoriteBackupError("unsupported_media_type")
        return candidate

    @staticmethod
    def _remote_path(root: str, relative: str) -> str:
        return safe_storage_relpath(f"{safe_storage_relpath(root)}/{safe_storage_relpath(relative)}")
