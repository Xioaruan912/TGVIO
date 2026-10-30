"""Telegram intake for the collector bot.

The bot has no menus: everything is driven from the web console. What remains
here is the input side (media, albums, Telegram links and web URLs the owner
forwards to the bot), the picks the console submits through the personal
session, and the durable scheduling of every Job (prepare, then the ordered
publish dispatcher). Each forwarded batch gets one short reply that points to
the console; failures reach the owner through the notification runtime.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Iterable

from telethon import TelegramClient, events
from telethon.tl import functions, types

from tgvio.adapters.telegram.intake_media import IntakeMediaMixin
from tgvio.application.intake import IncomingMedia, IntakeAcceptResult, IntakeService
from tgvio.application.job_control import JobCancelRequested, JobHoldRequested
from tgvio.application.job_runner import JobRunner
from tgvio.application.publish_styles import resolve_style
from tgvio.application.scheduler import OrderedPublishDispatcher, PhaseClaimGuard, PhaseClaimLostError
from tgvio.config import Settings
from tgvio.domain.intake import SpoilerMode
from tgvio.domain.job import Job
from tgvio.domain.telegram_links import is_telegram_link
from tgvio.observability import log_event


@dataclass(slots=True)
class _PendingBatch:
    chat_id: int
    sender_id: int
    media: list[IncomingMedia] = field(default_factory=list)
    first_seen: float = 0.0
    flush_task: asyncio.Task | None = None


class TelethonIntakeRuntime(IntakeMediaMixin):
    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        intake: IntakeService,
        processor: JobRunner,
        *,
        console_url: str = "",
    ) -> None:
        self._client = client
        self._settings = settings
        self._intake = intake
        self._processor = processor
        self._console_url = console_url.strip()
        self._source_reader = None
        self._source_owner_id: int | None = None
        self._log = logging.getLogger("tgvio.telegram.intake")
        self._tasks: set[asyncio.Task] = set()
        self._pending_batches: dict[tuple[int, int], _PendingBatch] = {}
        self._flush_tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(settings.worker_concurrency)
        self._repository = getattr(processor, "repository", None)
        self._dispatcher = (
            OrderedPublishDispatcher(self._repository, processor)
            if self._repository is not None and hasattr(processor, "prepare")
            else None
        )

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._dispatcher is not None:
            await self._dispatcher.start()

    def register(self) -> None:
        self._client.add_event_handler(self._on_album, events.Album())
        self._client.add_event_handler(
            self._on_message,
            events.NewMessage(incoming=True, func=lambda event: event.message.grouped_id is None),
        )

    async def configure_menu(self) -> None:
        """One command, /start, which answers with the console address."""

        try:
            await self._client(functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(), lang_code="",
                commands=[types.BotCommand(command="start", description="打开网页控制台")],
            ))
            await self._client(functions.bots.SetBotMenuButtonRequest(
                user_id=types.InputUserEmpty(), button=types.BotMenuButtonCommands(),
            ))
        except Exception as exc:  # noqa: BLE001 - the menu is cosmetic
            log_event(self._log, logging.WARNING, "telegram.menu.failed", exception_type=type(exc).__name__)

    async def stop(self) -> None:
        await self._flush_all_pending()
        pending = tuple(self._tasks)
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=30)
            except TimeoutError:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._dispatcher is not None:
            await self._dispatcher.stop()

    # ---------------------------------------------------------- personal source
    def set_source(self, client, reader, owner_id: int) -> None:
        del client
        self._source_reader = reader
        self._source_owner_id = int(owner_id)
        log_event(self._log, logging.INFO, "source.reader.ready", "Source reader attached",
                  resolved_chats=len(getattr(reader, "allowed_ids", ()) or ()))

    def clear_source(self) -> None:
        self._source_reader = None

    async def accept_source_media(
        self,
        owner_id: int,
        media,
        label: str = "",
        merge: bool = False,
        options: dict | None = None,
    ) -> tuple[int, int]:
        """Picks and links submitted from the web console; nothing is sent to the chat."""

        policy: dict = {}
        if str(label).strip():
            policy["source_label"] = str(label).strip()
        if merge:
            policy["merge_album"] = True
        options = options or {}
        if options.get("archive_only"):
            policy["archive_only"] = True
        return await self._accept_and_schedule(
            None, int(owner_id), media, policy_extra=policy, spoiler_mode=options.get("spoiler_mode"),
        )

    async def notify_source_owner(self, text: str) -> None:
        owner_id = self._source_owner_id or (self._settings.allowed_users[0] if self._settings.allowed_users else None)
        if owner_id is not None:
            await self._safe_send(int(owner_id), text)

    # ------------------------------------------------------------------ input
    async def _on_message(self, event) -> None:
        if not self._authorized(event.sender_id):
            return
        raw_text = (event.raw_text or "").strip()
        if raw_text.startswith("/"):
            if raw_text.split()[0].split("@", 1)[0].lower() == "/start":
                await self._safe_send(event.chat_id, self._console_text("把视频、图片或链接转发给我就会开始下载。"))
            raise events.StopPropagation
        incoming = self._from_message(event.message, event.chat_id)
        if incoming is not None:
            await self._queue_batch(event.chat_id, event.sender_id, [incoming])
            return
        if self._source_reader is not None and raw_text and is_telegram_link(raw_text):
            media = await self._source_reader.resolve_link(raw_text)
            if media:
                await self._accept_and_schedule(int(event.chat_id), int(event.sender_id), media)
            else:
                await self._safe_send(event.chat_id, "⚠️ 读不到这个 Telegram 链接：可能你的账号不在那个频道，或消息已删除。")
            return
        if self._settings.url_enabled:
            try:
                incoming = self._from_url_message(event.message, event.chat_id)
            except ValueError:
                await self._safe_send(event.chat_id, "🛡️ 这个链接不符合安全策略，已拒绝。")
                return
            if incoming is not None:
                await self._queue_batch(event.chat_id, event.sender_id, [incoming])
                return
        if raw_text:
            await self._safe_send(event.chat_id, self._console_text("这里只收视频、图片和链接。"))

    async def _on_album(self, event) -> None:
        if not event.messages or not self._authorized(event.messages[0].sender_id):
            return
        media = [
            incoming
            for incoming in (self._from_message(message, event.chat_id) for message in event.messages)
            if incoming is not None
        ]
        if media:
            await self._queue_batch(event.chat_id, event.messages[0].sender_id, media)

    def _console_text(self, lead: str) -> str:
        if not self._console_url:
            return lead
        return f"{lead}\n挑选频道里的视频、看进度和设置都在控制台：{self._console_url}"

    # --------------------------------------------------------------- batching
    async def _queue_batch(self, chat_id: int, sender_id: int, media: Iterable[IncomingMedia]) -> None:
        incoming = list(media)
        if not incoming:
            return
        window_ms = int(getattr(self._settings, "batch_window_ms", 0))
        if window_ms <= 0:
            await self._accept_and_schedule(chat_id, sender_id, incoming)
            return
        key = (int(chat_id), int(sender_id))
        loop = asyncio.get_running_loop()
        pending = self._pending_batches.get(key)
        if pending is None:
            pending = _PendingBatch(chat_id=int(chat_id), sender_id=int(sender_id), first_seen=loop.time())
            self._pending_batches[key] = pending
        pending.media.extend(incoming)
        pending.media.sort(key=lambda item: (
            item.source_message_id if item.source_message_id is not None else 2**63,
            item.grouped_id if item.grouped_id is not None else 2**63,
        ))
        max_items = max(1, int(getattr(self._settings, "batch_max_items", 100)))
        while len(pending.media) >= max_items:
            chunk = pending.media[:max_items]
            del pending.media[:max_items]
            await self._accept_and_schedule(pending.chat_id, pending.sender_id, chunk)
            pending.first_seen = loop.time()
        if not pending.media:
            if pending.flush_task is not None:
                pending.flush_task.cancel()
            self._pending_batches.pop(key, None)
            return
        self._schedule_batch_flush(key, pending)

    def _schedule_batch_flush(self, key: tuple[int, int], pending: _PendingBatch) -> None:
        if pending.flush_task is not None:
            pending.flush_task.cancel()
        loop = asyncio.get_running_loop()
        window = int(getattr(self._settings, "batch_window_ms", 1500))
        max_wait_ms = max(window, int(getattr(self._settings, "batch_max_wait_ms", 5000)))
        elapsed = max(0.0, loop.time() - pending.first_seen)
        delay = min(window / 1000.0, max(0.0, max_wait_ms / 1000.0 - elapsed))
        task = asyncio.create_task(self._flush_after(key, delay), name=f"tgvio-intake-batch-{key[0]}-{key[1]}")
        pending.flush_task = task
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _flush_after(self, key: tuple[int, int], delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await self._flush_batch(key)

    async def _flush_batch(self, key: tuple[int, int]) -> None:
        pending = self._pending_batches.pop(key, None)
        if pending is None or not pending.media:
            return
        current = asyncio.current_task()
        if pending.flush_task is not None and pending.flush_task is not current:
            pending.flush_task.cancel()
        await self._accept_and_schedule(pending.chat_id, pending.sender_id, pending.media)

    async def _flush_all_pending(self) -> None:
        keys = tuple(self._pending_batches)
        for pending in self._pending_batches.values():
            if pending.flush_task is not None:
                pending.flush_task.cancel()
        if self._flush_tasks:
            await asyncio.gather(*tuple(self._flush_tasks), return_exceptions=True)
        for key in keys:
            await self._flush_batch(key)

    # --------------------------------------------------------------- accepting
    async def _accept_and_schedule(
        self,
        chat_id: int | None,
        owner_id: int,
        media: Iterable[IncomingMedia],
        *,
        policy_extra: dict | None = None,
        spoiler_mode: SpoilerMode | None = None,
    ) -> tuple[int, int]:
        """Accept one batch and schedule it; returns ``(accepted_items, skipped_items)``.

        ``chat_id`` is the chat to acknowledge in, or None for console submissions.
        """

        batch = list(media)
        preference = await self._intake.get_user_preference(int(owner_id))
        policy: dict = dict(policy_extra or {})
        # The publish style chosen in the console applies to every new Job; with
        # none chosen the process baseline (COVER_MODE / FORWARD_CAPTION) is used.
        _name, style = resolve_style(preference.style_json)
        if style:
            policy["publish_style"] = style
        mode = spoiler_mode or preference.spoiler_mode
        if mode == SpoilerMode.ASK:  # asking needed a chat round-trip; the console has no such step
            mode = SpoilerMode.SOURCE
        accepted: IntakeAcceptResult = await self._intake.accept_once(
            owner_id=int(owner_id),
            destination=self._settings.destination,
            media=batch,
            policy=policy,
            spoiler_mode=mode,
        )
        if not accepted.created:
            log_event(self._log, logging.INFO, "intake.update.duplicate_suppressed",
                      job_id=accepted.job.id, item_count=len(batch))
            if chat_id is not None:
                await self._safe_send(chat_id, "这些内容之前已经收到过，不会重复下载。")
            return (0, len(batch))
        job = accepted.job
        number = await self._display_number(job.id)
        log_event(self._log, logging.INFO, "intake.job.accepted", job_id=job.id, job_no=number,
                  item_count=len(job.items), archive_only=bool(policy.get("archive_only")),
                  input_kinds=",".join(sorted({item.kind.value for item in job.items})))
        if chat_id is not None:
            label = f"任务 #{number}" if number is not None else "任务"
            await self._safe_send(chat_id, self._console_text(f"✅ 已收到 · {label} · {len(job.items)} 个媒体，开始下载。"))
        self.schedule(job)
        return (len(job.items), max(0, len(batch) - len(job.items)))

    async def _display_number(self, job_id: str) -> int | None:
        repository = self._repository
        if repository is None:
            return None
        try:
            value = await repository.get_display_no(job_id)
        except Exception:  # noqa: BLE001 - fall back to the durable order
            value = None
        if value is not None:
            return int(value)
        return await repository.get_accepted_order(job_id)

    # -------------------------------------------------------------- scheduling
    async def recover(self, job: Job) -> None:
        if self._intake.is_spoiler_pending(job):
            # A Job left waiting for an in-chat spoiler answer: there is no chat
            # prompt any more, so it continues the way a timeout always did.
            job = await self._intake.apply_spoiler_decision(job, spoiler=False)
        self.schedule(job)

    def schedule(self, job: Job, **_ignored) -> None:
        task = asyncio.create_task(self._process_job(job), name=f"tgvio-job-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_job(self, job: Job) -> None:
        async with self._semaphore:
            repository = self._repository
            if repository is None or not hasattr(self._processor, "prepare"):
                await self._processor.process(job)
                return
            claim = PhaseClaimGuard(repository, job.id, "prepare")
            if not await claim.start():
                log_event(self._log, logging.INFO, "scheduler.prepare.duplicate_suppressed", job_id=job.id)
                if self._dispatcher is not None:
                    self._dispatcher.notify()
                return
            try:
                try:
                    completed = await claim.run(self._processor.prepare(job))
                except asyncio.CancelledError:
                    raise
                except PhaseClaimLostError:
                    log_event(self._log, logging.WARNING, "scheduler.prepare.claim_lost",
                              "Preparation stopped because its durable claim was lost", job_id=job.id)
                    return
                except JobCancelRequested:
                    log_event(self._log, logging.WARNING, "job.run.cancelled", job_id=job.id)
                    return
                except JobHoldRequested:
                    log_event(self._log, logging.INFO, "job.run.held",
                              "Preparation stopped at a safe boundary because the Job is held", job_id=job.id)
                    return
                except Exception as exc:
                    log_event(self._log, logging.ERROR, "job.run.failed", "Job failed during intake preparation",
                              job_id=job.id, exception_type=type(exc).__name__, exc_info=True)
                    return
            finally:
                await claim.stop()
            log_event(self._log, logging.INFO, "job.prepare.result", job_id=completed.id, state=completed.state.value)
            if self._dispatcher is not None:
                self._dispatcher.notify()

    # ----------------------------------------------------------------- helpers
    def _authorized(self, sender_id: int | None) -> bool:
        return sender_id is not None and int(sender_id) in self._settings.allowed_users

    async def _safe_send(self, chat_id: int, text: str):
        try:
            return await self._client.send_message(chat_id, text, link_preview=False)
        except Exception as exc:
            log_event(self._log, logging.WARNING, "telegram.send.failed", "Failed to send a message",
                      exception_type=type(exc).__name__)
            return None
