"""Realtime source access checks, batching, and exact-id restart recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from telethon.tl.types import (
    DocumentAttributeVideo,
    InputPeerChannel,
    InputPeerChat,
    MessageMediaDocument,
    MessageMediaPhoto,
)
from telethon.utils import get_peer_id


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceAccessResult:
    ok: bool
    code: str
    message: str
    source_peer_id: int | None = None


@dataclass(frozen=True)
class SourceRuntimeStartupReport:
    checked_profiles: int = 0
    inaccessible_profiles: int = 0
    restored_events: int = 0
    failed_events: int = 0
    deferred_events: int = 0


def _access_error(exc: Exception) -> SourceAccessResult:
    name = exc.__class__.__name__
    if name in {"UserNotParticipantError", "ParticipantIdInvalidError"}:
        return SourceAccessResult(False, "bot_not_member", "Bot 不是该来源的有效成员")
    if name in {"ChannelPrivateError", "ChatAdminRequiredError", "ChannelInvalidError"}:
        return SourceAccessResult(False, "source_private", "Bot 无权访问该来源")
    if name in {
        "UsernameInvalidError",
        "UsernameNotOccupiedError",
        "PeerIdInvalidError",
        "ValueError",
    }:
        return SourceAccessResult(False, "source_not_found", "找不到该来源")
    return SourceAccessResult(False, "source_access_failed", "来源访问检查失败")


async def probe_source_access(
    client: Any,
    source_peer: str,
    *,
    expected_peer_id: int | None = None,
) -> SourceAccessResult:
    """Verify that the running Bot itself is a participant, without leaking peer data."""
    try:
        entity = await client.get_input_entity(str(source_peer).strip())
        if not isinstance(entity, (InputPeerChannel, InputPeerChat)):
            return SourceAccessResult(
                False,
                "source_type_unsupported",
                "来源必须是频道或群组",
            )
        source_peer_id = int(get_peer_id(entity))
        if expected_peer_id is not None and source_peer_id != int(expected_peer_id):
            return SourceAccessResult(
                False,
                "source_identity_changed",
                "来源标识与已保存配置不一致",
            )
        me = await client.get_me(input_peer=True)
        await client.get_permissions(entity, me)
        return SourceAccessResult(True, "ok", "Bot 可以访问该来源", source_peer_id)
    except Exception as exc:
        return _access_error(exc)


def is_supported_source_media(message: Any) -> bool:
    return isinstance(
        getattr(message, "media", None),
        (MessageMediaPhoto, MessageMediaDocument),
    )


def is_video_message(message: Any) -> bool:
    media = getattr(message, "media", None)
    if not isinstance(media, MessageMediaDocument):
        return False
    document = getattr(media, "document", None)
    mime_type = str(getattr(document, "mime_type", "") or "").lower()
    if mime_type.startswith("video/"):
        return True
    return any(
        isinstance(attribute, DocumentAttributeVideo)
        for attribute in (getattr(document, "attributes", None) or ())
    )


@dataclass
class _BufferedItem:
    message: Any
    event_id: int
    received_at: float

    @property
    def source_message_id(self) -> int:
        return int(getattr(self.message, "id", 0) or 0)


@dataclass
class _SourceBundle:
    profile: Any
    kind: str
    key: int | tuple[int, int]
    first_received_at: float
    last_received_at: float
    items: list[_BufferedItem] = field(default_factory=list)
    timer: asyncio.Task | None = None
    generation: int = 0


class SourceProfileRuntime:
    """Serialize source media into native albums and restart-safe video batches."""

    def __init__(
        self,
        *,
        client: Any,
        sources: Any,
        enqueue: Callable[[Any, list[Any], list[int]], Awaitable[None]],
        access_probe: Callable[..., Awaitable[SourceAccessResult]] = probe_source_access,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_batch_size: int = 10,
    ) -> None:
        self._client = client
        self._sources = sources
        self._enqueue = enqueue
        self._access_probe = access_probe
        self._clock = clock
        self._sleep = sleep
        self._max_batch_size = min(10, max(1, int(max_batch_size)))
        self._native: dict[tuple[int, int], _SourceBundle] = {}
        self._sequential: dict[int, _SourceBundle] = {}
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._startup_report = SourceRuntimeStartupReport()

    @property
    def pending_bundle_count(self) -> int:
        return len(self._native) + len(self._sequential)

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _cancel_timer(bundle: _SourceBundle) -> None:
        task = bundle.timer
        bundle.timer = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _schedule_locked(self, bundle: _SourceBundle, delay: float) -> None:
        self._cancel_timer(bundle)
        bundle.generation += 1
        generation = bundle.generation
        task = asyncio.create_task(
            self._flush_after(bundle.kind, bundle.key, generation, max(0.0, float(delay)))
        )
        bundle.timer = task
        self._track(task)

    def _lookup_locked(self, kind: str, key: int | tuple[int, int]) -> _SourceBundle | None:
        if kind == "native":
            return self._native.get(key)  # type: ignore[arg-type]
        return self._sequential.get(int(key))

    def _pop_locked(self, bundle: _SourceBundle) -> None:
        if bundle.kind == "native":
            self._native.pop(bundle.key, None)  # type: ignore[arg-type]
        else:
            self._sequential.pop(int(bundle.key), None)

    async def _enqueue_bundle_locked(self, bundle: _SourceBundle) -> None:
        self._cancel_timer(bundle)
        items = sorted(
            bundle.items,
            key=lambda item: (item.source_message_id, item.event_id),
        )
        if not items:
            return
        messages = [item.message for item in items]
        event_ids = [item.event_id for item in items]
        try:
            await self._enqueue(bundle.profile, messages, event_ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.__class__.__name__
            await self._sources.mark_failed(event_ids, code)
            logger.warning(
                "Unexpected source enqueue failure profile=%s code=%s",
                bundle.profile.id,
                code,
            )

    async def _flush_bundle_locked(self, bundle: _SourceBundle) -> None:
        self._pop_locked(bundle)
        await self._enqueue_bundle_locked(bundle)

    async def _flush_after(
        self,
        kind: str,
        key: int | tuple[int, int],
        generation: int,
        delay: float,
    ) -> None:
        try:
            await self._sleep(delay)
            async with self._lock:
                if self._closed:
                    return
                bundle = self._lookup_locked(kind, key)
                if bundle is None or bundle.generation != generation:
                    return
                bundle.timer = None
                await self._flush_bundle_locked(bundle)
        except asyncio.CancelledError:
            return

    async def _flush_profile_locked(self, profile_id: int) -> None:
        bundles = [
            bundle
            for bundle in [
                self._sequential.get(int(profile_id)),
                *(
                    bundle
                    for (candidate_id, _grouped_id), bundle in self._native.items()
                    if candidate_id == int(profile_id)
                ),
            ]
            if bundle is not None
        ]
        bundles.sort(key=lambda bundle: (bundle.first_received_at, str(bundle.key)))
        for bundle in bundles:
            if self._lookup_locked(bundle.kind, bundle.key) is bundle:
                await self._flush_bundle_locked(bundle)

    async def _flush_other_native_locked(
        self,
        profile_id: int,
        keep_key: tuple[int, int] | None = None,
    ) -> None:
        bundles = [
            bundle
            for key, bundle in self._native.items()
            if key[0] == int(profile_id) and key != keep_key
        ]
        bundles.sort(key=lambda bundle: (bundle.first_received_at, str(bundle.key)))
        for bundle in bundles:
            if self._native.get(bundle.key) is bundle:
                await self._flush_bundle_locked(bundle)

    async def _accept_locked(
        self,
        profile: Any,
        message: Any,
        event_id: int,
        *,
        grouped_id: int | None,
        received_at: float,
    ) -> None:
        profile_id = int(profile.id)
        item = _BufferedItem(message, int(event_id), float(received_at))
        now = self._clock()

        if grouped_id is not None:
            sequential = self._sequential.get(profile_id)
            if sequential is not None:
                await self._flush_bundle_locked(sequential)
            key = (profile_id, int(grouped_id))
            await self._flush_other_native_locked(profile_id, keep_key=key)
            bundle = self._native.get(key)
            if bundle is None:
                bundle = _SourceBundle(
                    profile=profile,
                    kind="native",
                    key=key,
                    first_received_at=item.received_at,
                    last_received_at=item.received_at,
                )
                self._native[key] = bundle
                delay = item.received_at + float(profile.album_gather_seconds) - now
                self._schedule_locked(bundle, delay)
            bundle.items.append(item)
            bundle.last_received_at = max(bundle.last_received_at, item.received_at)
            if len(bundle.items) >= self._max_batch_size:
                await self._flush_bundle_locked(bundle)
            return

        await self._flush_other_native_locked(profile_id)
        if is_video_message(message):
            window = float(profile.sequential_video_gather_seconds)
            bundle = self._sequential.get(profile_id)
            if bundle is not None and item.received_at - bundle.last_received_at >= window:
                await self._flush_bundle_locked(bundle)
                bundle = None
            if bundle is None:
                bundle = _SourceBundle(
                    profile=profile,
                    kind="sequential",
                    key=profile_id,
                    first_received_at=item.received_at,
                    last_received_at=item.received_at,
                )
                self._sequential[profile_id] = bundle
            bundle.items.append(item)
            bundle.last_received_at = max(bundle.last_received_at, item.received_at)
            if len(bundle.items) >= self._max_batch_size:
                await self._flush_bundle_locked(bundle)
            else:
                delay = bundle.last_received_at + window - now
                self._schedule_locked(bundle, delay)
            return

        await self._flush_profile_locked(profile_id)
        immediate = _SourceBundle(
            profile=profile,
            kind="immediate",
            key=profile_id,
            first_received_at=item.received_at,
            last_received_at=item.received_at,
            items=[item],
        )
        await self._enqueue_bundle_locked(immediate)

    async def accept(
        self,
        profile: Any,
        message: Any,
        event_id: int,
        *,
        grouped_id: int | None,
        received_at: float | None = None,
    ) -> None:
        if not is_supported_source_media(message):
            await self.boundary(profile)
            await self._sources.mark_failed([int(event_id)], "source_media_unavailable")
            return
        async with self._lock:
            if self._closed:
                return
            await self._accept_locked(
                profile,
                message,
                int(event_id),
                grouped_id=grouped_id,
                received_at=self._clock() if received_at is None else float(received_at),
            )

    async def boundary(self, profile: Any) -> None:
        async with self._lock:
            if self._closed:
                return
            await self._flush_profile_locked(int(profile.id))

    async def _notify_inaccessible(self, profile: Any, result: SourceAccessResult) -> None:
        try:
            await self._client.send_message(
                profile.owner_user_id,
                f"⚠️ 自动来源“{str(profile.name)[:48]}”当前无法接收：{result.message}。"
                "请把 Bot 加为来源频道管理员后，在 /sources 点击“检查访问”。",
                parse_mode=None,
            )
        except Exception:
            pass

    @staticmethod
    def _message_map(fetched: Any) -> dict[int, Any]:
        if fetched is None:
            values: list[Any] = []
        elif isinstance(fetched, (list, tuple)):
            values = list(fetched)
        else:
            values = [fetched]
        return {
            int(getattr(message, "id", 0)): message
            for message in values
            if message is not None and int(getattr(message, "id", 0) or 0) > 0
        }

    async def _recover_profile(self, profile: Any, events: list[Any]) -> tuple[int, int]:
        message_ids = [int(event.source_message_id) for event in events]
        fetched = await self._client.get_messages(profile.source_peer, ids=message_ids)
        messages = self._message_map(fetched)
        restored = 0
        failed = 0
        async with self._lock:
            if self._closed:
                return 0, 0
            for event in sorted(events, key=lambda value: (value.created_at, value.id)):
                message = messages.get(int(event.source_message_id))
                if message is None or not is_supported_source_media(message):
                    await self._flush_profile_locked(int(profile.id))
                    await self._sources.mark_failed([event.id], "source_message_unavailable")
                    failed += 1
                    continue
                await self._accept_locked(
                    profile,
                    message,
                    event.id,
                    grouped_id=event.grouped_id,
                    received_at=event.created_at,
                )
                restored += 1

            now = self._clock()
            sequential = self._sequential.get(int(profile.id))
            if (
                sequential is not None
                and sequential.last_received_at
                + float(profile.sequential_video_gather_seconds)
                <= now
            ):
                await self._flush_bundle_locked(sequential)
            for key, bundle in list(self._native.items()):
                if (
                    key[0] == int(profile.id)
                    and bundle.first_received_at + float(profile.album_gather_seconds) <= now
                ):
                    await self._flush_bundle_locked(bundle)
        return restored, failed

    async def startup(self) -> SourceRuntimeStartupReport:
        if self._started:
            return self._startup_report
        self._started = True
        profiles = await self._sources.list_profiles()
        events: list[Any] = []
        after_created_at: float | None = None
        after_id = 0
        while True:
            page = await self._sources.received_events(
                limit=5000,
                after_created_at=after_created_at,
                after_id=after_id,
            )
            events.extend(page)
            if len(page) < 5000:
                break
            last = page[-1]
            after_created_at = float(last.created_at)
            after_id = int(last.id)
        profiles_by_id = {int(profile.id): profile for profile in profiles}
        pending_by_profile: dict[int, list[Any]] = {}
        for event in events:
            pending_by_profile.setdefault(int(event.source_profile_id), []).append(event)

        checked = inaccessible = restored = failed = deferred = 0
        orphan_ids = [
            event.id
            for event in events
            if int(event.source_profile_id) not in profiles_by_id
        ]
        if orphan_ids:
            await self._sources.mark_failed(orphan_ids, "source_profile_unavailable")
            failed += len(orphan_ids)

        for profile in profiles:
            pending = pending_by_profile.get(int(profile.id), [])
            if not profile.enabled and not pending:
                continue
            checked += 1
            result = await self._access_probe(
                self._client,
                profile.source_peer,
                expected_peer_id=profile.source_peer_id,
            )
            if not result.ok:
                inaccessible += 1
                deferred += len(pending)
                logger.warning(
                    "Source access unavailable profile=%s code=%s pending=%s",
                    profile.id,
                    result.code,
                    len(pending),
                )
                await self._notify_inaccessible(profile, result)
                continue
            if not pending:
                continue
            try:
                restored_now, failed_now = await self._recover_profile(profile, pending)
            except Exception as exc:
                deferred += len(pending)
                logger.warning(
                    "Source exact-id recovery deferred profile=%s code=%s pending=%s",
                    profile.id,
                    exc.__class__.__name__,
                    len(pending),
                )
                await self._notify_inaccessible(
                    profile,
                    SourceAccessResult(
                        False,
                        "source_recovery_failed",
                        "已接收消息暂时无法恢复",
                    ),
                )
            else:
                restored += restored_now
                failed += failed_now

        self._startup_report = SourceRuntimeStartupReport(
            checked_profiles=checked,
            inaccessible_profiles=inaccessible,
            restored_events=restored,
            failed_events=failed,
            deferred_events=deferred,
        )
        return self._startup_report

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            bundles = [*self._native.values(), *self._sequential.values()]
            self._native.clear()
            self._sequential.clear()
            for bundle in bundles:
                self._cancel_timer(bundle)
            tasks = {task for task in self._tasks if not task.done()}
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
