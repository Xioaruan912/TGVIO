"""Owner-driven source reader backed by the personal-account session.

The owner picks content from the TGVIO chat (``/grab`` or ``/pick``) or sends a
Telegram message link; this reader reads that message — including the whole
media group when it is an album — and turns it into durable ``IncomingMedia``.
It never listens to source chats on its own.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
from typing import Sequence

from telethon import utils

from tgvio.domain.job import MediaKind
from tgvio.domain.telegram_links import TelegramLink, parse_telegram_link
from tgvio.application.intake import IncomingMedia
from tgvio.adapters.telegram.media_downloader import TelethonMediaDownloader

_ALBUM_SPAN = 11
_LATEST_SCAN = 20
_LIST_WINDOW = 400
_THUMBNAIL_MAX_BYTES = 1024 * 1024
_PHOTO_FALLBACK_MAX_BYTES = 2 * 1024 * 1024
_VIDEO_PARTIAL_MAX_BYTES = 4 * 1024 * 1024
_VIDEO_PARTIAL_TIMEOUT = 6.0


@dataclass(frozen=True, slots=True)
class ThumbnailCandidate:
    """A local preview source: an image, or a video fragment needing a frame."""

    path: Path
    needs_frame: bool = False


@dataclass(frozen=True, slots=True)
class SourceMediaSummary:
    """One selectable item: a single media message or a full media group."""

    message_id: int
    kinds: tuple[MediaKind, ...]
    item_count: int
    size_bytes: int
    duration_seconds: float | None
    date: datetime | None
    grouped_id: int | None
    video_count: int = 0
    photo_count: int = 0

    @property
    def has_video(self) -> bool:
        if self.video_count:
            return True
        return any(kind in {MediaKind.VIDEO, MediaKind.AUDIO} for kind in self.kinds)

    @property
    def is_photo_only(self) -> bool:
        if self.item_count and self.photo_count == self.item_count:
            return True
        return all(kind == MediaKind.PHOTO for kind in self.kinds)


class UserSourceDownloader:
    """Routes ``user_source`` items through whichever personal client is ready.

    The session may be authorized after startup (in-Bot login), so the delegate
    downloader is resolved lazily instead of being bound once at wiring time.
    """

    def __init__(
        self,
        coordinator,
        *,
        download_workers: int = 4,
        part_size_kb: int = 512,
        shard_retries: int = 3,
    ) -> None:
        self._coordinator = coordinator
        self._download_workers = max(1, int(download_workers))
        self._part_size_kb = int(part_size_kb)
        self._shard_retries = max(0, int(shard_retries))

    def _delegate(self) -> TelethonMediaDownloader:
        client = getattr(self._coordinator, "client", None)
        if client is None:
            raise RuntimeError("source session is not ready")
        return TelethonMediaDownloader(
            client,
            download_workers=self._download_workers,
            part_size_kb=self._part_size_kb,
            shard_retries=self._shard_retries,
        )

    async def download(self, item, target_dir, progress_callback=None):
        return await self._delegate().download(item, target_dir, progress_callback)

    async def download_bounded(self, item, target_dir, *, max_bytes):
        return await self._delegate().download_bounded(item, target_dir, max_bytes=max_bytes)


class UserSourceReader:
    def __init__(
        self,
        client,
        *,
        allowed_chats: Sequence[str] | None = None,
    ) -> None:
        self._client = client
        self._allowed_raw = tuple(entry for entry in (allowed_chats or ()) if str(entry).strip())
        self._allowed_ids: set[int] = set()
        self._chat_labels: dict[int, str] = {}
        self._log = logging.getLogger("tgvio.telegram.source")

    def label_for(self, chat_id: int) -> str:
        return self._chat_labels.get(int(chat_id), str(chat_id))

    def ordered_chats(self) -> list[int]:
        """Whitelist chats in configuration order (best-effort)."""

        ordered: list[int] = []
        for entry in self._allowed_raw:
            for chat_id, label in self._chat_labels.items():
                if label == entry and chat_id not in ordered:
                    ordered.append(chat_id)
        for chat_id in sorted(self._allowed_ids):
            if chat_id not in ordered:
                ordered.append(chat_id)
        return ordered

    @property
    def allowed_ids(self) -> frozenset[int]:
        return frozenset(self._allowed_ids)

    async def prepare(self) -> int:
        """Resolve the whitelist to durable chat ids; unresolvable entries are skipped."""

        resolved: set[int] = set()
        labels: dict[int, str] = {}
        for entry in self._allowed_raw:
            try:
                entity = await self._client.get_entity(entry)
                peer_id = int(utils.get_peer_id(entity))
                resolved.add(peer_id)
                labels[peer_id] = str(entry)
            except Exception as exc:  # noqa: BLE001 - a bad entry must not abort startup
                self._log.warning(
                    "source whitelist entry could not be resolved: %s", type(exc).__name__
                )
        self._allowed_ids = resolved
        self._chat_labels = labels
        return len(resolved)

    # ------------------------------------------------------------------ list
    async def list_recent_media(
        self,
        chat_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[SourceMediaSummary], bool]:
        """Newest-first media groups; albums collapse into a single entry."""

        page_size = max(1, int(limit))
        start = max(0, int(offset))
        want = start + page_size + 1
        summaries: list[SourceMediaSummary] = []
        position_by_group: dict[int, int] = {}
        try:
            async for message in self._client.iter_messages(
                int(chat_id),
                limit=_LIST_WINDOW,
            ):
                item = self._to_media(message)
                if item is None:
                    continue
                grouped = item.grouped_id
                if grouped is not None and grouped in position_by_group:
                    position = position_by_group[grouped]
                    summaries[position] = self._merge(summaries[position], message, item)
                else:
                    if grouped is not None:
                        position_by_group[grouped] = len(summaries)
                    summaries.append(self._summarize(message, item))
                if len(summaries) >= want:
                    break
        except Exception as exc:  # noqa: BLE001 - user-facing miss
            self._log.warning("source.list.failed type=%s", type(exc).__name__)
            return ([], False)
        page = summaries[start : start + page_size]
        has_more = len(summaries) > start + page_size
        return (page, has_more)

    # --------------------------------------------------------------- capture
    async def capture_latest(
        self,
        chat_id: int,
        *,
        photo_only: bool = False,
        limit: int = _LATEST_SCAN,
    ) -> list[IncomingMedia]:
        """Capture the newest media group, preferring real content over photos."""

        page, _has_more = await self.list_recent_media(chat_id, limit=limit)
        if photo_only:
            candidates = list(page)
        else:
            candidates = [summary for summary in page if not summary.is_photo_only]
            if not candidates:
                candidates = list(page)
        for summary in candidates:
            captured = await self.capture_at(chat_id, summary.message_id)
            if captured:
                return captured
        return []

    async def capture_at(self, chat_id: int, message_id: int | None) -> list[IncomingMedia]:
        if message_id is None:
            return []
        try:
            message = await self._client.get_messages(int(chat_id), ids=int(message_id))
        except Exception as exc:  # noqa: BLE001 - user-facing miss
            self._log.warning("source.capture.failed type=%s", type(exc).__name__)
            return []
        if message is None:
            return []
        return self._to_media_list(await self._expand(message))

    async def fetch_thumbnail(
        self,
        chat_id: int,
        message_id: int,
        target_dir: Path,
    ) -> ThumbnailCandidate | None:
        """Resolve a cheap visual preview for one message.

        Order: the embedded server thumbnail (10-50 KB) first; for photos without
        one, the original image (bounded); for videos without one, the first few
        megabytes plus a frame extraction request. Never returns unbounded work.
        """

        try:
            message = await self._client.get_messages(int(chat_id), ids=int(message_id))
        except Exception as exc:  # noqa: BLE001 - a missing preview is not fatal
            self._log.warning("source.thumb.fetch_failed type=%s", type(exc).__name__)
            return None
        if message is None:
            return None
        target = Path(target_dir)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        if self._message_has_thumbnail(message):
            candidate = await self._download_thumb(message, target, int(message_id))
            if candidate is not None:
                return candidate
        return await self._download_fallback(message, target, int(message_id))

    async def _download_thumb(
        self,
        message,
        target: Path,
        message_id: int,
    ) -> ThumbnailCandidate | None:
        path = target / f"thumb-{message_id}.jpg"
        try:
            downloaded = await self._client.download_media(
                message,
                file=str(path),
                thumb=-1,
            )
        except Exception as exc:  # noqa: BLE001 - previews degrade to no image
            self._log.info("source.thumb.download_failed type=%s", type(exc).__name__)
            return None
        result = Path(downloaded) if downloaded else path
        if not self._usable(result, _THUMBNAIL_MAX_BYTES):
            return None
        return ThumbnailCandidate(path=result)

    async def _download_fallback(
        self,
        message,
        target: Path,
        message_id: int,
    ) -> ThumbnailCandidate | None:
        if getattr(message, "photo", None) is not None:
            remote_size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
            if remote_size > _PHOTO_FALLBACK_MAX_BYTES:
                return None
            path = target / f"photo-{message_id}.jpg"
            if await self._stream_limited(message, path, _PHOTO_FALLBACK_MAX_BYTES, 20.0):
                if remote_size and path.stat().st_size != remote_size:
                    # A truncated image cannot be decoded; do not pretend it is one.
                    path.unlink(missing_ok=True)
                    return None
                self._log.info("source.thumb.fallback kind=photo message_id=%s", message_id)
                return ThumbnailCandidate(path=path)
            return None
        if self._is_video(message):
            path = target / f"clip-{message_id}.mp4"
            if await self._stream_limited(
                message, path, _VIDEO_PARTIAL_MAX_BYTES, _VIDEO_PARTIAL_TIMEOUT
            ):
                self._log.info("source.thumb.fallback kind=video message_id=%s", message_id)
                return ThumbnailCandidate(path=path, needs_frame=True)
        return None

    async def _stream_limited(
        self,
        message,
        path: Path,
        limit: int,
        timeout: float,
    ) -> bool:
        """Write at most ``limit`` bytes of the media within ``timeout`` seconds."""

        media = getattr(message, "media", None)
        if media is None:
            return False

        async def _run() -> bool:
            written = 0
            iterator = self._client.iter_download(media, request_size=256 * 1024)
            try:
                with path.open("xb") as handle:
                    async for chunk in iterator:
                        payload = bytes(chunk)
                        if not payload:
                            continue
                        remaining = limit - written
                        if remaining <= 0:
                            break
                        handle.write(payload[:remaining])
                        written += min(len(payload), remaining)
                        if written >= limit:
                            break
            finally:
                close = getattr(iterator, "close", None) or getattr(iterator, "aclose", None)
                if close is not None:
                    with suppress(Exception):
                        await close()
            return written > 0

        try:
            ok = await asyncio.wait_for(_run(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            ok = path.is_file() and path.stat().st_size > 0
        except Exception as exc:  # noqa: BLE001 - previews are best effort
            self._log.info("source.thumb.stream_failed type=%s", type(exc).__name__)
            ok = False
        if not ok:
            path.unlink(missing_ok=True)
            return False
        return True

    @staticmethod
    def _is_video(message) -> bool:
        if getattr(message, "video", None) is not None:
            return True
        document = getattr(message, "document", None)
        if document is None:
            return False
        return str(getattr(document, "mime_type", "") or "").startswith("video")

    @staticmethod
    def _usable(path: Path, limit: int) -> bool:
        try:
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size <= 0
                or path.stat().st_size > limit
            ):
                path.unlink(missing_ok=True)
                return False
        except OSError:
            return False
        return True

    @staticmethod
    def _message_has_thumbnail(message) -> bool:
        photo = getattr(message, "photo", None)
        if photo is not None:
            return bool(getattr(photo, "sizes", None))
        document = getattr(message, "document", None)
        if document is not None:
            return bool(getattr(document, "thumbs", None))
        return False

    async def resolve_link(self, url: str) -> list[IncomingMedia]:
        link: TelegramLink | None = parse_telegram_link(url)
        if link is None:
            return []
        try:
            entity = await self._client.get_entity(link.chat)
            message = await self._client.get_messages(entity, ids=link.message_id)
        except Exception as exc:  # noqa: BLE001 - unreadable link is a user-facing miss
            self._log.warning("source link could not be resolved: %s", type(exc).__name__)
            return []
        if message is None:
            return []
        return self._to_media_list(await self._expand(message))

    async def _expand(self, message):
        grouped = getattr(message, "grouped_id", None)
        if not grouped:
            return [message]
        try:
            nearby = [
                item
                async for item in self._client.iter_messages(
                    message.chat_id,
                    min_id=max(0, int(message.id) - _ALBUM_SPAN),
                    max_id=int(message.id) + _ALBUM_SPAN,
                )
            ]
        except Exception:  # noqa: BLE001 - fall back to the single message
            return [message]
        album = [item for item in nearby if getattr(item, "grouped_id", None) == grouped]
        album.sort(key=lambda item: int(item.id))
        return album or [message]

    # ---------------------------------------------------------------- helpers
    @classmethod
    def _summarize(cls, message, item: IncomingMedia) -> SourceMediaSummary:
        return SourceMediaSummary(
            message_id=int(item.source_message_id or message.id),
            kinds=(item.kind,),
            item_count=1,
            size_bytes=int(item.size_bytes or 0),
            duration_seconds=cls._message_duration(message),
            date=getattr(message, "date", None),
            grouped_id=item.grouped_id,
            video_count=1 if item.kind == MediaKind.VIDEO else 0,
            photo_count=1 if item.kind == MediaKind.PHOTO else 0,
        )

    @classmethod
    def _merge(cls, summary: SourceMediaSummary, message, item: IncomingMedia) -> SourceMediaSummary:
        kinds = tuple(dict.fromkeys((*summary.kinds, item.kind)))
        duration = summary.duration_seconds or cls._message_duration(message)
        return SourceMediaSummary(
            message_id=summary.message_id,
            kinds=kinds,
            item_count=summary.item_count + 1,
            size_bytes=summary.size_bytes + int(item.size_bytes or 0),
            duration_seconds=duration,
            date=summary.date or getattr(message, "date", None),
            grouped_id=summary.grouped_id,
            video_count=summary.video_count + (1 if item.kind == MediaKind.VIDEO else 0),
            photo_count=summary.photo_count + (1 if item.kind == MediaKind.PHOTO else 0),
        )

    @staticmethod
    def _message_duration(message) -> float | None:
        for attribute in ("video", "audio", "voice"):
            media = getattr(message, attribute, None)
            duration = getattr(media, "duration", None)
            if duration:
                try:
                    return float(duration)
                except (TypeError, ValueError):
                    continue
        return None

    def _to_media_list(self, messages) -> list[IncomingMedia]:
        media: list[IncomingMedia] = []
        for message in messages:
            item = self._to_media(message)
            if item is not None:
                media.append(item)
        return media

    @staticmethod
    def _to_media(message) -> IncomingMedia | None:
        if getattr(message, "photo", None) is not None:
            kind = MediaKind.PHOTO
        elif getattr(message, "video", None) is not None:
            kind = MediaKind.VIDEO
        elif getattr(message, "audio", None) is not None:
            kind = MediaKind.AUDIO
        elif getattr(message, "voice", None) is not None:
            kind = MediaKind.AUDIO
        elif getattr(message, "document", None) is not None:
            kind = MediaKind.DOCUMENT
        else:
            return None
        file_info = getattr(message, "file", None)
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if chat_id is None or message_id is None:
            return None
        return IncomingMedia(
            kind=kind,
            source=f"telegram:{int(chat_id)}:{int(message_id)}",
            caption=str(getattr(message, "message", "") or ""),
            size_bytes=int(getattr(file_info, "size", 0) or 0),
            name=getattr(file_info, "name", None),
            spoiler=bool(getattr(getattr(message, "media", None), "spoiler", False)),
            grouped_id=getattr(message, "grouped_id", None),
            source_chat_id=int(chat_id),
            source_message_id=int(message_id),
            metadata={"source_type": "user_source", "user_source": True},
        )
