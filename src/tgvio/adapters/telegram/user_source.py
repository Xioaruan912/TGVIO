"""Owner-driven "push" reader backed by the personal-account session.

The owner points at content explicitly — by replying to a message with the
configured trigger, or by sending a Telegram message link — and this reader
turns that single message (plus its media group, when present) into durable
``IncomingMedia`` items. It never scans chats on its own.
"""

from __future__ import annotations

import logging
from typing import Sequence

from telethon import utils

from tgvio.domain.job import MediaKind
from tgvio.domain.telegram_links import TelegramLink, parse_telegram_link
from tgvio.application.intake import IncomingMedia
from tgvio.adapters.telegram.media_downloader import TelethonMediaDownloader

_ALBUM_SPAN = 11
_LATEST_SCAN = 25
_REPLAY_WINDOW = 5
_SEED_SCAN = 120


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
        trigger: str = "#tgvio",
        allowed_chats: Sequence[str] | None = None,
    ) -> None:
        self._client = client
        self._trigger = (trigger or "#tgvio").strip() or "#tgvio"
        self._allowed_raw = tuple(entry for entry in (allowed_chats or ()) if str(entry).strip())
        self._allowed_ids: set[int] = set()
        self._chat_labels: dict[int, str] = {}
        self.seed_report: dict[str, int] = {}
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
    def trigger(self) -> str:
        return self._trigger

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

    def matches_trigger(self, text: str | None) -> bool:
        return (text or "").strip().casefold() == self._trigger.casefold()

    def is_trigger(self, event) -> bool:
        if not getattr(event, "outgoing", False):
            return False
        text = getattr(event, "raw_text", "") or ""
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return False
        if int(chat_id) in self._allowed_ids:
            self._log.info(
                "source.update.outgoing trigger=%s",
                self.matches_trigger(text),
            )
        else:
            if self.matches_trigger(text):
                self._log.info("source.trigger.rejected reason=not_whitelisted")
            return False
        return self.matches_trigger(text)

    async def capture_latest(self, chat_id: int, *, limit: int = _LATEST_SCAN) -> list[IncomingMedia]:
        """Capture the newest media message in a whitelisted chat."""

        try:
            async for message in self._client.iter_messages(int(chat_id), limit=max(1, int(limit))):
                item = self._to_media(message)
                if item is not None:
                    return self._to_media_list(await self._expand(message))
        except Exception as exc:  # noqa: BLE001 - user-facing miss
            self._log.warning("source.latest.failed type=%s", type(exc).__name__)
            return []
        return []

    async def capture_reply(self, event) -> list[IncomingMedia]:
        return await self.capture_at(
            int(event.chat_id),
            getattr(getattr(event, "message", None), "reply_to_msg_id", None),
        )

    async def capture_at(self, chat_id: int, reply_to_msg_id: int | None) -> list[IncomingMedia]:
        if reply_to_msg_id is None:
            return []
        try:
            message = await self._client.get_messages(int(chat_id), ids=int(reply_to_msg_id))
        except Exception as exc:  # noqa: BLE001 - user-facing miss
            self._log.warning("source.reply.failed type=%s", type(exc).__name__)
            return []
        if message is None:
            return []
        return self._to_media_list(await self._expand(message))

    async def find_recent_triggers(
        self,
        *,
        limit: int = 20,
    ) -> list[tuple[int, int, int | None]]:
        """Server-side search for your trigger messages in every whitelisted chat."""

        found: list[tuple[int, int, int | None]] = []
        for chat_id in sorted(self._allowed_ids):
            try:
                async for message in self._client.iter_messages(
                    chat_id,
                    limit=max(1, int(limit)),
                    search=self._trigger,
                ):
                    if not getattr(message, "outgoing", False):
                        continue
                    if not self.matches_trigger(getattr(message, "message", "")):
                        continue
                    found.append(
                        (int(chat_id), int(message.id), getattr(message, "reply_to_msg_id", None))
                    )
            except Exception as exc:  # noqa: BLE001
                self._log.warning("source.search.failed type=%s", type(exc).__name__)
        found.sort(key=lambda item: item[1], reverse=True)
        return found

    async def seed_trigger_cursor(self, *, limit: int = _SEED_SCAN) -> dict[int, int]:
        """Cursor per chat that also picks up recent, not-yet-handled triggers.

        Uses a server-side search so a trigger is found even when the source bot
        has sent far more than the scan window since. Handled triggers are
        deleted, and intake dedupes by ``(chat_id, message_id)``, so this can
        never double-publish.
        """

        cursor: dict[int, int] = {}
        matches: dict[int, list[int]] = {}
        triggers = 0
        for chat_id in sorted(self._allowed_ids):
            try:
                async for message in self._client.iter_messages(
                    chat_id,
                    limit=max(1, int(limit)),
                    search=self._trigger,
                ):
                    if not getattr(message, "outgoing", False):
                        continue
                    if not self.matches_trigger(getattr(message, "message", "")):
                        continue
                    triggers += 1
                    matches.setdefault(chat_id, []).append(int(message.id))
            except Exception:  # noqa: BLE001
                continue
        for chat_id in sorted(self._allowed_ids):
            newest: int | None = None
            try:
                async for message in self._client.iter_messages(chat_id, limit=1):
                    newest = int(message.id)
                    break
            except Exception:  # noqa: BLE001
                continue
            if newest is None:
                continue
            chat_matches = matches.get(chat_id) or []
            if chat_matches:
                cursor[chat_id] = max(0, min(chat_matches) - 1)
            else:
                cursor[chat_id] = max(0, newest - _REPLAY_WINDOW)
        self.seed_report = {"chats": len(cursor), "triggers": triggers}
        return cursor

    async def poll_triggers(
        self,
        after: dict[int, int],
        *,
        limit: int = 10,
    ) -> list[tuple[int, int, int | None]]:
        """Find new outgoing trigger messages; a fallback when updates are delayed."""

        found: list[tuple[int, int, int | None]] = []
        for chat_id in sorted(self._allowed_ids):
            try:
                async for message in self._client.iter_messages(chat_id, limit=max(1, int(limit))):
                    if not getattr(message, "outgoing", False):
                        continue
                    if not self.matches_trigger(getattr(message, "message", "")):
                        continue
                    if int(message.id) <= int(after.get(chat_id, 0)):
                        continue
                    found.append(
                        (int(chat_id), int(message.id), getattr(message, "reply_to_msg_id", None))
                    )
            except Exception:  # noqa: BLE001
                continue
        return found

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
        except Exception:  # noqa: BLE001 - fall back to the single replied message
            return [message]
        album = [item for item in nearby if getattr(item, "grouped_id", None) == grouped]
        album.sort(key=lambda item: int(item.id))
        return album or [message]

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
