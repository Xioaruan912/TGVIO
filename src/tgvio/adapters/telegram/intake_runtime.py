from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Iterable

from telethon import Button, TelegramClient, events

from tgvio.adapters.telegram.user_messages import (
    describe_archive_failure,
    describe_job_failure,
)
from tgvio.application.intake import (
    CollectionEmptyError,
    IncomingMedia,
    IntakeAcceptResult,
    IntakeService,
)
from tgvio.application.auto_recovery import (
    archive_failure_waits_for_recovery,
    archive_recovery_state,
    job_failure_waits_for_recovery,
    job_recovery_state,
)
from tgvio.application.job_control import JobCancelRequested, JobHoldRequested
from tgvio.application.job_runner import JobRunner
from tgvio.application.scheduler import (
    OrderedPublishDispatcher,
    PhaseClaimGuard,
    PhaseClaimLostError,
)
from tgvio.config import Settings
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.intake import JobDisplayMessage, SpoilerMode
from tgvio.domain.job import Job, JobState, MediaKind
from tgvio.domain.progress import JobProgress
from tgvio.infrastructure.url_security import validate_url_syntax
from tgvio.observability import log_event


COLLECTION_BEGIN_BUTTON = "📥 开始合集"
COLLECTION_END_BUTTON = "🛑 结束并发布"


@dataclass(slots=True)
class _PendingBatch:
    chat_id: int
    sender_id: int
    media: list[IncomingMedia] = field(default_factory=list)
    first_seen: float = 0.0
    flush_task: asyncio.Task | None = None


class TelethonIntakeRuntime:
    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        intake: IntakeService,
        processor: JobRunner,
    ) -> None:
        self._client = client
        self._settings = settings
        self._intake = intake
        self._processor = processor
        self._log = logging.getLogger("tgvio.telegram.intake")
        self._tasks: set[asyncio.Task] = set()
        self._status_tasks: dict[str, asyncio.Task] = {}
        self._confirmation_tasks: dict[str, asyncio.Task] = {}
        self._confirmation_lock = asyncio.Lock()
        self._pending_batches: dict[tuple[int, int], _PendingBatch] = {}
        self._flush_tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(settings.worker_concurrency)
        repository = getattr(processor, "repository", None)
        if repository is None:
            repository = getattr(intake, "repository", None)
        self._repository = repository
        self._dispatcher = (
            OrderedPublishDispatcher(repository, processor)
            if repository is not None and hasattr(processor, "prepare")
            else None
        )

    async def start(self) -> None:
        if self._dispatcher is not None:
            await self._dispatcher.start()

    def register(self) -> None:
        self._client.add_event_handler(self._on_album, events.Album())
        self._client.add_event_handler(
            self._on_message,
            events.NewMessage(incoming=True, func=lambda event: event.message.grouped_id is None),
        )
        self._client.add_event_handler(
            self._on_intake_command,
            events.NewMessage(
                incoming=True,
                func=lambda event: (event.raw_text or "").lstrip().startswith("/"),
            ),
        )
        self._client.add_event_handler(
            self._on_collection_button,
            events.NewMessage(
                incoming=True,
                func=lambda event: (event.raw_text or "").strip()
                in {COLLECTION_BEGIN_BUTTON, COLLECTION_END_BUTTON},
            ),
        )
        self._client.add_event_handler(
            self._on_intake_callback,
            events.CallbackQuery(pattern=b"^intake:"),
        )

    async def stop(self) -> None:
        await self._flush_all_pending()
        if not self._tasks:
            pending = ()
        else:
            pending = tuple(self._tasks)
        try:
            if pending:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=30,
                )
        except TimeoutError:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self._dispatcher is not None:
            await self._dispatcher.stop()
        if self._status_tasks:
            status_tasks = tuple(self._status_tasks.values())
            for task in status_tasks:
                task.cancel()
            await asyncio.gather(*status_tasks, return_exceptions=True)
            self._status_tasks.clear()
        if self._confirmation_tasks:
            confirmation_tasks = tuple(self._confirmation_tasks.values())
            for task in confirmation_tasks:
                task.cancel()
            await asyncio.gather(*confirmation_tasks, return_exceptions=True)
            self._confirmation_tasks.clear()

    async def _on_message(self, event) -> None:
        if not self._authorized(event.sender_id):
            return
        raw_text = (event.raw_text or "").strip()
        if raw_text.lstrip().startswith("/") or raw_text in {
            COLLECTION_BEGIN_BUTTON,
            COLLECTION_END_BUTTON,
        }:
            return

        incoming = self._from_message(event.message, event.chat_id)
        if incoming is not None:
            session = await self._open_collection(event.sender_id, event.chat_id)
            if session is not None:
                inserted = await self._intake.add_collection_media(session, [incoming])
                if inserted:
                    log_event(
                        self._log,
                        logging.INFO,
                        "intake.collection.media_added",
                        collection_id=session.id,
                        media_added=inserted,
                    )
                return
            await self._queue_batch(event.chat_id, event.sender_id, [incoming])
            return

        # URL intake deliberately remains outside explicit collection sessions,
        # matching the legacy behaviour and avoiding hidden yt-dlp work at /end.
        if self._settings.url_enabled:
            try:
                incoming = self._from_url_message(event.message, event.chat_id)
            except ValueError:
                await self._safe_send(event.chat_id, "🛡️ URL 已拒绝：不符合 TGVIO 安全策略。")
                return
            if incoming is not None:
                await self._queue_batch(event.chat_id, event.sender_id, [incoming])
                return

        session = await self._open_collection(event.sender_id, event.chat_id)
        if session is not None and raw_text:
            inserted = await self._intake.add_collection_text(
                session,
                text=raw_text,
                source_chat_id=int(event.chat_id),
                source_message_id=int(event.message.id),
            )
            if inserted:
                log_event(
                    self._log,
                    logging.INFO,
                    "intake.collection.text_added",
                    collection_id=session.id,
                )

    async def _on_album(self, event) -> None:
        if not event.messages:
            return
        sender_id = event.messages[0].sender_id
        if not self._authorized(sender_id):
            return
        media = [
            incoming
            for incoming in (self._from_message(message, event.chat_id) for message in event.messages)
            if incoming is not None
        ]
        if not media:
            return
        session = await self._open_collection(sender_id, event.chat_id)
        if session is not None:
            inserted = await self._intake.add_collection_media(session, media)
            if inserted:
                log_event(
                    self._log,
                    logging.INFO,
                    "intake.collection.album_added",
                    collection_id=session.id,
                    media_added=inserted,
                )
            return
        await self._queue_batch(event.chat_id, sender_id, media)

    async def _open_collection(self, sender_id: int, chat_id: int):
        if not bool(getattr(self._settings, "collections_enabled", True)):
            return None
        if not hasattr(self._intake, "open_collection"):
            return None
        return await self._intake.open_collection(
            owner_id=int(sender_id),
            chat_id=int(chat_id),
        )

    async def _on_intake_command(self, event) -> None:
        if not self._authorized(event.sender_id):
            return
        raw = (event.raw_text or "").strip()
        head, _, argument = raw.partition(" ")
        command = head[1:].split("@", 1)[0].lower()
        if command in {"begin", "开始"}:
            await self._begin_collection(event.chat_id, int(event.sender_id))
        elif command in {"end", "结束"}:
            await self._end_collection(event.chat_id, int(event.sender_id))
        elif command == "mode":
            requested = argument.strip().lower()
            aliases = {
                "source": SpoilerMode.SOURCE,
                "inherit": SpoilerMode.SOURCE,
                "ask": SpoilerMode.ASK,
                "spoiler": SpoilerMode.ALWAYS_SPOILER,
                "always_spoiler": SpoilerMode.ALWAYS_SPOILER,
                "normal": SpoilerMode.ALWAYS_NORMAL,
                "always_normal": SpoilerMode.ALWAYS_NORMAL,
            }
            if requested in aliases:
                await self._intake.set_spoiler_mode(int(event.sender_id), aliases[requested])
            await self._show_mode(event.chat_id, int(event.sender_id))
        else:
            return
        raise events.StopPropagation

    async def _on_collection_button(self, event) -> None:
        if not self._authorized(event.sender_id):
            return
        action = (event.raw_text or "").strip()
        if action == COLLECTION_BEGIN_BUTTON:
            await self._begin_collection(event.chat_id, int(event.sender_id))
        elif action == COLLECTION_END_BUTTON:
            await self._end_collection(event.chat_id, int(event.sender_id))
        raise events.StopPropagation

    async def _on_intake_callback(self, event) -> None:
        if not self._authorized(event.sender_id):
            await self._safe_answer(event, "无权限", alert=True)
            return
        action = bytes(event.data or b"").decode("utf-8", "replace")
        owner_id = int(event.sender_id)
        if action.startswith("intake:mode:"):
            raw_mode = action.rsplit(":", 1)[-1]
            try:
                mode = SpoilerMode(raw_mode)
            except ValueError:
                await self._safe_answer(event, "模式无效", alert=True)
                return
            await self._intake.set_spoiler_mode(owner_id, mode)
            await self._safe_answer(event, "模式已更新")
            try:
                await event.edit(self._mode_text(mode), buttons=self._mode_buttons(), parse_mode="md")
            except Exception:
                pass
            return
        if action.startswith("intake:spoiler:"):
            parts = action.split(":", 3)
            if len(parts) != 4:
                await self._safe_answer(event, "操作无效", alert=True)
                return
            await self._handle_spoiler_choice(
                event,
                owner_id=owner_id,
                job_id=parts[2],
                choice=parts[3],
            )
            return
        if action.startswith("intake:end:"):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return
            await self._safe_answer(event, "正在结束合集")
            await self._end_collection(event.chat_id, owner_id)
            return
        if action.startswith("intake:collection-cancel:"):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return
            await self._intake.cancel_collection(owner_id=owner_id, chat_id=int(event.chat_id))
            await self._safe_answer(event, "合集已取消")
            if session.status_message_id is not None:
                await self._safe_edit(
                    int(event.chat_id),
                    int(session.status_message_id),
                    "⛔ **合集已取消**",
                )

    async def _begin_collection(self, chat_id: int, owner_id: int) -> None:
        if not bool(getattr(self._settings, "collections_enabled", True)):
            await self._safe_send(chat_id, "合集功能当前未启用。")
            return
        if not hasattr(self._intake, "begin_collection"):
            await self._safe_send(chat_id, "合集功能当前不可用。")
            return
        key = (int(chat_id), int(owner_id))
        if key in self._pending_batches:
            await self._flush_batch(key)
        session = await self._intake.begin_collection(owner_id=owner_id, chat_id=int(chat_id))
        media_count, text_count = await self._intake.collection_counts(session.id)
        text = self._collection_status_text(media_count, text_count)
        buttons = self._collection_buttons(session.id)
        if session.status_message_id is None:
            message = await self._safe_send(chat_id, text, buttons=buttons)
            message_id = getattr(message, "id", None)
            if message_id is not None:
                await self._repository.set_collection_status_message(
                    session.id,
                    int(chat_id),
                    int(message_id),
                )
        else:
            await self._safe_edit(
                int(session.status_chat_id or chat_id),
                int(session.status_message_id),
                text,
                buttons=buttons,
            )

    async def _end_collection(self, chat_id: int, owner_id: int) -> None:
        if not bool(getattr(self._settings, "collections_enabled", True)):
            await self._safe_send(chat_id, "合集功能当前未启用。")
            return
        preference = await self._intake.get_user_preference(owner_id)
        try:
            result = await self._intake.finalize_collection(
                owner_id=owner_id,
                chat_id=int(chat_id),
                destination=self._settings.destination,
                max_items=min(100, int(getattr(self._settings, "batch_max_items", 100))),
                spoiler_mode=preference.spoiler_mode,
                ask_timeout_seconds=int(
                    getattr(self._settings, "spoiler_confirm_timeout_seconds", 60)
                ),
            )
        except CollectionEmptyError as exc:
            if "no open" in str(exc):
                await self._safe_send(chat_id, "当前没有正在收集的合集。")
            else:
                await self._safe_send(chat_id, "合集里还没有媒体；继续发送媒体后再结束。")
            return

        reused_status = False
        activated = 0
        for accepted in result.jobs:
            if accepted.job.terminal:
                continue
            activated += 1
            if accepted.created:
                if (
                    not reused_status
                    and result.session.status_message_id is not None
                    and result.session.status_chat_id is not None
                ):
                    await self._save_display_message(
                        accepted.job.id,
                        int(result.session.status_chat_id),
                        int(result.session.status_message_id),
                    )
                    await self._announce_job(
                        int(result.session.status_chat_id),
                        accepted.job,
                        status_message_id=int(result.session.status_message_id),
                    )
                    reused_status = True
                else:
                    await self._announce_job(int(chat_id), accepted.job)
            else:
                # A previous /end attempt may have committed this Job but
                # crashed before scheduling it. Durable phase claims make
                # recovery safe even if another local task is already active.
                await self.recover(accepted.job)
        if activated == 0 and result.session.status_message_id is not None:
            await self._safe_edit(
                int(result.session.status_chat_id or chat_id),
                int(result.session.status_message_id),
                "✅ **合集已结束**\n没有新的媒体需要创建任务；重复 update 已忽略。",
            )

    async def _show_mode(self, chat_id: int, owner_id: int) -> None:
        preference = await self._intake.get_user_preference(owner_id)
        await self._safe_send(
            chat_id,
            self._mode_text(preference.spoiler_mode),
            buttons=self._mode_buttons(),
        )

    @staticmethod
    def _mode_text(mode: SpoilerMode) -> str:
        labels = {
            SpoilerMode.SOURCE: "跟随原消息",
            SpoilerMode.ASK: "每次询问",
            SpoilerMode.ALWAYS_SPOILER: "总是雪花遮挡",
            SpoilerMode.ALWAYS_NORMAL: "总是正常显示",
        }
        return (
            "**媒体显示模式**\n\n"
            f"当前：`{labels[mode]}`\n\n"
            "默认跟随原消息已有的 spoiler；ask 模式会在任务进入队列后询问一次，超时自动按正常显示处理。"
        )

    @staticmethod
    def _mode_buttons():
        return [
            [Button.inline("↩️ 跟随原消息", b"intake:mode:source")],
            [
                Button.inline("❓ 每次询问", b"intake:mode:ask"),
                Button.inline("🌨️ 总是雪花", b"intake:mode:always_spoiler"),
            ],
            [Button.inline("✅ 总是正常", b"intake:mode:always_normal")],
        ]

    @staticmethod
    def _collection_status_text(media_count: int, text_count: int) -> str:
        return (
            "📥 **合集收集中**\n\n"
            f"媒体：`{media_count}`\n"
            f"文字：`{text_count}`\n\n"
            "继续发送图片、视频或文字；完成后点“结束并发布”或发送 /end。"
        )

    @staticmethod
    def _collection_buttons(session_id: str):
        return [
            [Button.inline("🛑 结束并发布", f"intake:end:{session_id}".encode("utf-8"))],
            [Button.inline("❌ 取消合集", f"intake:collection-cancel:{session_id}".encode("utf-8"))],
        ]

    async def _queue_batch(
        self,
        chat_id: int,
        sender_id: int,
        media: Iterable[IncomingMedia],
    ) -> None:
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
            pending = _PendingBatch(
                chat_id=int(chat_id),
                sender_id=int(sender_id),
                first_seen=loop.time(),
            )
            self._pending_batches[key] = pending
        pending.media.extend(incoming)
        pending.media.sort(
            key=lambda item: (
                item.source_message_id if item.source_message_id is not None else 2**63,
                item.grouped_id if item.grouped_id is not None else 2**63,
            )
        )
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
        max_wait_ms = max(
            int(getattr(self._settings, "batch_window_ms", 1500)),
            int(getattr(self._settings, "batch_max_wait_ms", 5000)),
        )
        elapsed = max(0.0, loop.time() - pending.first_seen)
        delay = min(
            int(getattr(self._settings, "batch_window_ms", 1500)) / 1000.0,
            max(0.0, max_wait_ms / 1000.0 - elapsed),
        )
        task = asyncio.create_task(
            self._flush_after(key, delay),
            name=f"tgvio-intake-batch-{key[0]}-{key[1]}",
        )
        pending.flush_task = task
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _flush_after(self, key: tuple[int, int], delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._flush_batch(key)
        except asyncio.CancelledError:
            raise

    async def _flush_batch(self, key: tuple[int, int]) -> None:
        pending = self._pending_batches.pop(key, None)
        if pending is None or not pending.media:
            return
        current = asyncio.current_task()
        if pending.flush_task is not None and pending.flush_task is not current:
            pending.flush_task.cancel()
        await self._accept_and_schedule(
            pending.chat_id,
            pending.sender_id,
            pending.media,
        )

    async def _flush_all_pending(self) -> None:
        keys = tuple(self._pending_batches)
        for pending in self._pending_batches.values():
            if pending.flush_task is not None:
                pending.flush_task.cancel()
        if self._flush_tasks:
            await asyncio.gather(*tuple(self._flush_tasks), return_exceptions=True)
        for key in keys:
            await self._flush_batch(key)

    async def _accept_and_schedule(
        self,
        chat_id: int,
        sender_id: int,
        media: Iterable[IncomingMedia],
    ) -> None:
        batch = list(media)
        if hasattr(self._intake, "accept_once") and hasattr(
            self._intake,
            "get_user_preference",
        ):
            preference = await self._intake.get_user_preference(int(sender_id))
            accepted = await self._intake.accept_once(
                owner_id=int(sender_id),
                destination=self._settings.destination,
                media=batch,
                policy={"display_expected": True},
                spoiler_mode=preference.spoiler_mode,
                ask_timeout_seconds=int(
                    getattr(self._settings, "spoiler_confirm_timeout_seconds", 60)
                ),
            )
        else:
            job = await self._intake.accept(
                owner_id=int(sender_id),
                destination=self._settings.destination,
                media=batch,
            )
            accepted = IntakeAcceptResult(job=job, created=True)

        if not accepted.created:
            log_event(
                self._log,
                logging.INFO,
                "intake.update.duplicate_suppressed",
                job_id=accepted.job.id,
                item_count=len(batch),
            )
            return
        job = accepted.job
        log_event(
            self._log,
            logging.INFO,
            "intake.job.accepted",
            job_id=job.id,
            item_count=len(job.items),
            input_kinds=",".join(sorted({item.kind.value for item in job.items})),
        )
        await self._announce_job(int(chat_id), job)

    async def _announce_job(
        self,
        chat_id: int,
        job: Job,
        *,
        status_message_id: int | None = None,
    ) -> None:
        if hasattr(self._intake, "is_spoiler_pending") and self._intake.is_spoiler_pending(job):
            await self._ensure_spoiler_confirmation(
                job,
                chat_id=chat_id,
                status_message_id=status_message_id,
            )
            return

        if status_message_id is None:
            status_message = await self._safe_send(chat_id, self._accepted_status_text(job))
            status_message_id = getattr(status_message, "id", None)
            if status_message_id is not None:
                await self._save_display_message(job.id, chat_id, int(status_message_id))
        else:
            await self._safe_edit(
                chat_id,
                int(status_message_id),
                self._accepted_status_text(job),
            )
        self.schedule(
            job,
            chat_id=chat_id,
            status_message_id=(int(status_message_id) if status_message_id is not None else None),
        )

    @staticmethod
    def _accepted_status_text(job: Job) -> str:
        return (
            f"✅ 已接收任务 `{job.id[:10]}`\n"
            f"媒体：{len(job.items)}\n"
            "正在下载和分析。"
        )

    async def _save_display_message(self, job_id: str, chat_id: int, message_id: int) -> None:
        if self._repository is None or not hasattr(self._repository, "save_job_display_message"):
            return
        await self._repository.save_job_display_message(
            JobDisplayMessage(
                job_id=job_id,
                chat_id=int(chat_id),
                message_id=int(message_id),
            )
        )

    async def recover(self, job: Job) -> None:
        ref = None
        if self._repository is not None and hasattr(self._repository, "get_job_display_message"):
            ref = await self._repository.get_job_display_message(job.id)
        chat_id = (
            ref.chat_id
            if ref is not None
            else next(
                (item.source_chat_id for item in job.items if item.source_chat_id is not None),
                None,
            )
        )
        if hasattr(self._intake, "is_spoiler_pending") and self._intake.is_spoiler_pending(job):
            await self._ensure_spoiler_confirmation(
                job,
                chat_id=chat_id,
                status_message_id=(ref.message_id if ref is not None else None),
            )
            return
        if (
            ref is None
            and bool(job.policy.get("display_expected"))
            and chat_id is not None
            and not job.terminal
        ):
            message = await self._safe_send(int(chat_id), self._accepted_status_text(job))
            message_id = getattr(message, "id", None)
            if message_id is not None:
                await self._save_display_message(job.id, int(chat_id), int(message_id))
                ref = JobDisplayMessage(job.id, int(chat_id), int(message_id))
        elif ref is not None and not job.terminal:
            ref = await self._edit_or_replace_display(
                job.id,
                ref,
                self._accepted_status_text(job),
            )
        self.schedule(
            job,
            chat_id=(int(chat_id) if chat_id is not None else None),
            status_message_id=(ref.message_id if ref is not None else None),
        )

    async def _ensure_spoiler_confirmation(
        self,
        job: Job,
        *,
        chat_id: int | None,
        status_message_id: int | None,
    ) -> None:
        if self._repository is None:
            return
        if chat_id is None:
            chat_id = next(
                (item.source_chat_id for item in job.items if item.source_chat_id is not None),
                None,
            )
        if status_message_id is None and hasattr(self._repository, "get_job_display_message"):
            ref = await self._repository.get_job_display_message(job.id)
            if ref is not None:
                chat_id = ref.chat_id
                status_message_id = ref.message_id
        if chat_id is None:
            return
        prompt = (
            f"❓ **任务 `{job.id[:10]}` 如何显示？**\n\n"
            f"媒体：`{len(job.items)}`\n"
            "请选择雪花遮挡或正常显示；超时会自动按正常显示处理。"
        )
        buttons = self._spoiler_buttons(job.id)
        if status_message_id is not None:
            ref = await self._repository.get_job_display_message(job.id)
            if ref is None:
                ref = JobDisplayMessage(
                    job_id=job.id,
                    chat_id=int(chat_id),
                    message_id=int(status_message_id),
                )
                await self._repository.save_job_display_message(ref)
            await self._edit_or_replace_display(
                job.id,
                ref,
                prompt,
                buttons=buttons,
            )
        else:
            message = await self._safe_send(int(chat_id), prompt, buttons=buttons)
            status_message_id = getattr(message, "id", None)
            if status_message_id is not None:
                await self._save_display_message(job.id, int(chat_id), int(status_message_id))
        existing = self._confirmation_tasks.get(job.id)
        if existing is None or existing.done():
            task = asyncio.create_task(
                self._spoiler_confirmation_timeout(job.id),
                name=f"tgvio-spoiler-confirm-{job.id}",
            )
            self._confirmation_tasks[job.id] = task
            task.add_done_callback(lambda _task, job_id=job.id: self._confirmation_tasks.pop(job_id, None))

    @staticmethod
    def _spoiler_buttons(job_id: str):
        return [
            [
                Button.inline("🌨️ 雪花遮挡", f"intake:spoiler:{job_id}:spoiler".encode("utf-8")),
                Button.inline("✅ 正常显示", f"intake:spoiler:{job_id}:normal".encode("utf-8")),
            ],
            [Button.inline("❌ 取消任务", f"intake:spoiler:{job_id}:cancel".encode("utf-8"))],
        ]

    async def _spoiler_confirmation_timeout(self, job_id: str) -> None:
        try:
            job = await self._repository.get(job_id)
            if job is None or not self._intake.is_spoiler_pending(job):
                return
            deadline = int(job.policy.get("spoiler_deadline_epoch", int(time.time())))
            delay = max(0.0, deadline - time.time())
            if delay:
                await asyncio.sleep(delay)
            async with self._confirmation_lock:
                job = await self._repository.get(job_id)
                if job is None or job.state != JobState.RECEIVED or not self._intake.is_spoiler_pending(job):
                    return
                job = await self._intake.apply_spoiler_decision(job, spoiler=False)
            log_event(
                self._log,
                logging.INFO,
                "intake.spoiler.timeout_normal",
                job_id=job.id,
            )
            await self._activate_confirmed_job(job)
        except asyncio.CancelledError:
            raise

    async def _handle_spoiler_choice(
        self,
        event,
        *,
        owner_id: int,
        job_id: str,
        choice: str,
    ) -> None:
        if self._repository is None:
            await self._safe_answer(event, "任务存储不可用", alert=True)
            return
        async with self._confirmation_lock:
            job = await self._repository.get(job_id)
            if job is None or job.owner_id != owner_id:
                await self._safe_answer(event, "任务不存在或无权限", alert=True)
                return
            if job.state != JobState.RECEIVED or not self._intake.is_spoiler_pending(job):
                await self._safe_answer(event, "这个选择已经失效", alert=True)
                return
            if choice == "cancel":
                await self._repository.transition(
                    job.id,
                    JobState.CANCELLED,
                    event_type="spoiler_confirmation_cancelled",
                    detail="cancelled by owner before processing",
                )
                resolved = None
            elif choice in {"spoiler", "normal"}:
                resolved = await self._intake.apply_spoiler_decision(
                    job,
                    spoiler=(choice == "spoiler"),
                )
            else:
                await self._safe_answer(event, "选择无效", alert=True)
                return
        timer = self._confirmation_tasks.pop(job_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        if resolved is None:
            ref = await self._repository.get_job_display_message(job_id)
            if ref is not None:
                await self._safe_edit(ref.chat_id, ref.message_id, f"⛔ 任务 `{job_id[:10]}` 已取消。")
            await self._safe_answer(event, "任务已取消")
            return
        await self._safe_answer(event, "已确认")
        await self._activate_confirmed_job(resolved)

    async def _activate_confirmed_job(self, job: Job) -> None:
        ref = (
            await self._repository.get_job_display_message(job.id)
            if self._repository is not None
            and hasattr(self._repository, "get_job_display_message")
            else None
        )
        chat_id = (
            ref.chat_id
            if ref is not None
            else next(
                (item.source_chat_id for item in job.items if item.source_chat_id is not None),
                None,
            )
        )
        if chat_id is not None and ref is not None:
            ref = await self._edit_or_replace_display(
                job.id,
                ref,
                self._accepted_status_text(job),
            )
        self.schedule(
            job,
            chat_id=(int(chat_id) if chat_id is not None else None),
            status_message_id=(ref.message_id if ref is not None else None),
        )

    async def _edit_or_replace_display(
        self,
        job_id: str,
        ref: JobDisplayMessage,
        text: str,
        *,
        buttons=None,
    ) -> JobDisplayMessage:
        if await self._safe_edit(
            ref.chat_id,
            ref.message_id,
            text,
            buttons=buttons,
        ):
            return ref
        if ref.replacement_count >= 1:
            return ref
        message = await self._safe_send(ref.chat_id, text, buttons=buttons)
        message_id = getattr(message, "id", None)
        if message_id is None:
            return ref
        replacement = JobDisplayMessage(
            job_id=job_id,
            chat_id=ref.chat_id,
            message_id=int(message_id),
            replacement_count=ref.replacement_count + 1,
        )
        return await self._repository.save_job_display_message(replacement)

    @staticmethod
    async def _safe_answer(event, text: str, *, alert: bool = False) -> None:
        try:
            await event.answer(text, alert=alert)
        except Exception:
            pass

    def schedule(
        self,
        job: Job,
        *,
        chat_id: int | None = None,
        status_message_id: int | None = None,
    ) -> None:
        if chat_id is None:
            chat_id = next((item.source_chat_id for item in job.items if item.source_chat_id is not None), None)
        if chat_id is not None and status_message_id is not None:
            existing = self._status_tasks.get(job.id)
            if existing is None or existing.done():
                tracker = asyncio.create_task(
                    self._track_status(int(chat_id), int(status_message_id), job.id),
                    name=f"tgvio-status-{job.id}",
                )
                self._status_tasks[job.id] = tracker
                tracker.add_done_callback(
                    lambda completed, job_id=job.id: self._forget_status_task(
                        job_id,
                        completed,
                    )
                )
        task = asyncio.create_task(
            self._process_job(chat_id, job, status_message_id=status_message_id),
            name=f"tgvio-job-{job.id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _forget_status_task(self, job_id: str, completed: asyncio.Task) -> None:
        if self._status_tasks.get(job_id) is completed:
            self._status_tasks.pop(job_id, None)

    async def _process_job(
        self,
        chat_id: int | None,
        job: Job,
        *,
        status_message_id: int | None = None,
    ) -> None:
        async with self._semaphore:
            repository = getattr(self._processor, "repository", None)
            if repository is None or not hasattr(self._processor, "prepare"):
                await self._processor.process(job)
                return
            claim = PhaseClaimGuard(repository, job.id, "prepare")
            if not await claim.start():
                log_event(
                    self._log,
                    logging.INFO,
                    "scheduler.prepare.duplicate_suppressed",
                    job_id=job.id,
                )
                if self._dispatcher is not None:
                    self._dispatcher.notify()
                return
            try:
                try:
                    completed = await claim.run(self._processor.prepare(job))
                except asyncio.CancelledError:
                    raise
                except PhaseClaimLostError:
                    log_event(
                        self._log,
                        logging.WARNING,
                        "scheduler.prepare.claim_lost",
                        "Preparation stopped because its durable claim was lost",
                        job_id=job.id,
                    )
                    return
                except JobCancelRequested:
                    log_event(
                        self._log,
                        logging.WARNING,
                        "job.run.cancelled",
                        job_id=job.id,
                    )
                    if chat_id is not None and status_message_id is None:
                        await self._safe_send(chat_id, f"⛔ 任务 `{job.id[:10]}` 已取消。")
                    return
                except JobHoldRequested:
                    log_event(
                        self._log,
                        logging.INFO,
                        "job.run.held",
                        "Preparation stopped at a safe boundary because the Job is held",
                        job_id=job.id,
                    )
                    return
                except Exception as exc:
                    log_event(
                        self._log,
                        logging.ERROR,
                        "job.run.failed",
                        "Job failed during intake preparation",
                        job_id=job.id,
                        exception_type=type(exc).__name__,
                        exc_info=True,
                    )
                    if chat_id is not None and status_message_id is None:
                        await self._safe_send(chat_id, f"❌ 任务 `{job.id[:10]}` 下载/分析失败。")
                    return
            finally:
                await claim.stop()
            log_event(
                self._log,
                logging.INFO,
                "job.prepare.result",
                job_id=completed.id,
                state=completed.state.value,
            )
            if self._dispatcher is not None:
                self._dispatcher.notify()
            if (
                chat_id is not None
                and status_message_id is None
                and completed.state == JobState.PLANNED
                and not bool(getattr(self._processor, "publish_enabled", False))
            ):
                await self._safe_send(
                    chat_id,
                    f"🧠 任务 `{completed.id[:10]}` 已完成分析与发布规划。\n"
                    "打开“📋 我的任务”即可查看发布计划。",
                )

    async def _track_status(self, chat_id: int, message_id: int, job_id: str) -> None:
        """Continuously edit the acceptance message with durable pipeline progress."""

        last_text = ""
        previous_item: int | None = None
        previous_current = 0
        previous_time = time.monotonic()
        try:
            while True:
                job = await self._intake.repository.get(job_id) if hasattr(self._intake, "repository") else None
                if job is None:
                    # IntakeService intentionally hides persistence details, so
                    # the runtime uses the processor's repository when exposed.
                    repository = getattr(self._processor, "repository", None)
                    if repository is None:
                        repository = getattr(self._processor, "_repository", None)
                    if repository is None:
                        return
                    job = await repository.get(job_id)
                else:
                    repository = self._intake.repository
                if job is None:
                    return
                progress = await repository.get_job_progress(job_id)
                archive = await repository.get_archive_package_for_job(job_id)
                control = await repository.get_job_control(job_id)
                held = bool(control.hold_requested)

                now = time.monotonic()
                speed_bps: float | None = None
                if progress is not None and progress.phase == "downloading":
                    if previous_item == progress.item_index and progress.current >= previous_current:
                        elapsed = now - previous_time
                        delta = progress.current - previous_current
                        if elapsed > 0 and delta > 0:
                            speed_bps = delta / elapsed
                    previous_item = progress.item_index
                    previous_current = progress.current
                    previous_time = now
                else:
                    previous_item = None
                    previous_current = 0
                    previous_time = now

                text = self._render_live_status(
                    job,
                    progress,
                    archive,
                    speed_bps=speed_bps,
                    held=held,
                )
                if text != last_text:
                    if await self._safe_edit(
                        chat_id,
                        message_id,
                        text,
                        buttons=self._status_buttons(job, archive),
                    ):
                        last_text = text
                if self._status_is_terminal(job, archive):
                    return
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "telegram.status_tracker.failed",
                "Dynamic job status tracker failed",
                job_id=job_id,
                exception_type=type(exc).__name__,
                exc_info=True,
            )

    def _render_live_status(
        self,
        job: Job,
        progress: JobProgress | None,
        archive: ArchivePackage | None,
        *,
        speed_bps: float | None = None,
        held: bool = False,
    ) -> str:
        total_bytes = sum(max(0, int(item.size_bytes or 0)) for item in job.items)
        lines = [
            f"✅ 已接收任务 `{job.id[:10]}`",
            f"媒体：`{len(job.items)}` · `{self._human_bytes(total_bytes)}`",
        ]

        phase = progress.phase if progress is not None else job.state.value
        if held and not job.terminal:
            lines.append("状态：⏸ **已暂停** · 已保留当前缓存，恢复后从安全边界继续")
        elif phase == "downloading" and progress is not None:
            completed_indexes = {item.index for item in job.items if item.local_path}
            completed_bytes = sum(
                max(0, int(item.size_bytes or 0))
                for item in job.items
                if item.index in completed_indexes
            )
            current_bytes = 0 if progress.item_index in completed_indexes else progress.current
            overall_done = min(total_bytes, completed_bytes + current_bytes) if total_bytes else 0
            overall_pct = int(overall_done * 100 / total_bytes) if total_bytes else 0
            item_pct = int(progress.current * 100 / progress.total) if progress.total else 0
            lines.extend(
                [
                    "状态：⬇️ **正在下载**",
                    f"总进度：`{self._progress_bar(overall_pct)}` `{overall_pct}%` · "
                    f"`{self._human_bytes(overall_done)}/{self._human_bytes(total_bytes)}`",
                    f"当前：`{(progress.item_index or 0) + 1}/{progress.item_total or len(job.items)}` · "
                    f"`{item_pct}%` · `{self._human_bytes(progress.current)}/{self._human_bytes(progress.total)}`",
                ]
            )
            if speed_bps is not None and speed_bps > 0:
                lines.append(f"速度：`{self._human_bytes(int(speed_bps))}/s`")
        elif phase == "analyzing" and progress is not None:
            current = min(progress.total, progress.current + 1) if progress.total else 0
            lines.extend(
                [
                    "状态：🔎 **正在分析**",
                    f"进度：`{current}/{progress.total or len(job.items)}`",
                ]
            )
        elif phase == "publishing" and progress is not None:
            current = min(progress.total, progress.current + 1) if progress.total else 0
            lines.extend(
                [
                    "状态：📤 **正在发布**",
                    f"步骤：`{current}/{progress.total}`",
                ]
            )
        elif job.state == JobState.SUCCEEDED:
            lines.append("状态：✅ **Telegram 发布完成**")
        elif job.state == JobState.FAILED:
            issue = describe_job_failure(job.error_code)
            recovery = job_recovery_state(job)
            recovery_status = str(recovery.get("status", ""))
            if recovery_status == "scheduled":
                lines.extend(
                    [
                        "状态：🔄 **系统正在自动恢复**",
                        f"原因：{issue.title}",
                        f"将自动进行第 `{recovery.get('next_attempt', '?')}/{recovery.get('max_attempts', '?')}` 次安全重试，无需操作。",
                    ]
                )
            elif recovery_status == "quarantined":
                lines.extend(
                    [
                        "状态：🛡️ **已隔离，不会自动重发**",
                        issue.explanation,
                        "后续任务会继续；请有空时核对目标频道中的实际消息。",
                    ]
                )
            elif recovery_status == "manual_review":
                lines.extend(
                    [
                        "状态：🛡️ **安全检查阻止重发**",
                        issue.explanation,
                        "此任务已跳过，后续任务会继续；需要管理员核对发布记录。",
                    ]
                )
            elif recovery_status in {"exhausted", "abandoned"}:
                lines.extend(
                    [
                        f"状态：⏭️ **{issue.title}，已自动跳过**",
                        f"自动处理未能恢复任务（已尝试 `{recovery.get('attempt_count', 0)}` 次）。",
                        "后续任务会继续；如仍需要这份内容，可在详情中手动重试或重新转发。",
                    ]
                )
            elif job_failure_waits_for_recovery(job):
                lines.extend(
                    [
                        "状态：🔄 **系统正在判断恢复方式**",
                        f"原因：{issue.title}",
                        "无需操作，系统会自动重试或安全跳过。",
                    ]
                )
            else:
                lines.extend(
                    [
                        f"状态：❌ **{issue.title}**",
                        issue.explanation,
                        f"下一步：{issue.action}",
                    ]
                )
        elif job.state == JobState.CANCELLED:
            lines.append("状态：⛔ **已取消**")
        elif job.state == JobState.PLANNED:
            if getattr(self._settings, "publish_enabled", False):
                lines.append("状态：🧠 **已规划，等待发布**")
            else:
                lines.append("状态：🧠 **已规划，自动发布关闭**")
        elif job.state in {JobState.DOWNLOADED, JobState.ANALYZED}:
            lines.append("状态：⏳ **准备下一阶段**")
        else:
            lines.append("状态：⏳ **等待处理**")

        if archive is not None:
            stored = sum(1 for obj in archive.objects if obj.state.value == "stored")
            archive_labels = {
                ArchivePackageState.PLANNED: "等待归档",
                ArchivePackageState.STAGING: "准备归档",
                ArchivePackageState.UPLOADING: "归档上传中",
                ArchivePackageState.VERIFYING: "归档校验中",
                ArchivePackageState.COMMITTED: "归档完成",
                ArchivePackageState.FAILED: "归档失败",
                ArchivePackageState.CANCELLED: "归档已取消",
            }
            icon = {
                ArchivePackageState.COMMITTED: "✅",
                ArchivePackageState.FAILED: "❌",
                ArchivePackageState.CANCELLED: "⛔",
            }.get(archive.state, "☁️")
            lines.append(
                f"WebDAV 归档：{icon} `{archive_labels[archive.state]}` · `{stored}/{len(archive.objects)}`"
            )
            if archive.state == ArchivePackageState.FAILED:
                issue = describe_archive_failure(archive.error_code)
                recovery = archive_recovery_state(job)
                recovery_status = str(recovery.get("status", ""))
                if recovery_status == "scheduled":
                    lines.extend(
                        [
                            issue.explanation,
                            f"系统将自动进行第 `{recovery.get('next_attempt', '?')}/{recovery.get('max_attempts', '?')}` 次续传，无需操作。",
                        ]
                    )
                elif recovery_status in {"exhausted", "abandoned"}:
                    lines.extend(
                        [
                            issue.explanation,
                            "自动续传已停止；Telegram 发布不受影响，可稍后从详情手动重传。",
                        ]
                    )
                elif archive_failure_waits_for_recovery(job, archive):
                    lines.extend([issue.explanation, "系统正在自动判断续传方式，无需操作。"])
                else:
                    lines.extend([issue.explanation, f"下一步：{issue.action}"])
        return "\n".join(lines)

    @staticmethod
    def _status_buttons(job: Job, archive: ArchivePackage | None):
        rows = [
            [
                Button.inline(
                    "🔎 查看任务",
                    f"ui:job:{job.id}".encode("utf-8"),
                )
            ]
        ]
        if (
            job.state == JobState.FAILED
            and job.error_code not in {"publish_partial", "publish_uncertain"}
            and not job_failure_waits_for_recovery(job)
            and job_recovery_state(job).get("status") != "manual_review"
        ):
            rows[0].append(
                Button.inline(
                    "🔁 重试任务",
                    f"ui:retry:{job.id}".encode("utf-8"),
                )
            )
        if (
            archive is not None
            and archive.state == ArchivePackageState.FAILED
            and not archive_failure_waits_for_recovery(job, archive)
        ):
            rows.append(
                [
                    Button.inline(
                        "☁️ 重传失败归档",
                        f"ui:archive-retry:{job.id}".encode("utf-8"),
                    )
                ]
            )
        return rows

    def _status_is_terminal(self, job: Job, archive: ArchivePackage | None) -> bool:
        if job_failure_waits_for_recovery(job):
            return False
        if archive_failure_waits_for_recovery(job, archive):
            return False
        archive_terminal = archive is None or archive.state in {
            ArchivePackageState.COMMITTED,
            ArchivePackageState.FAILED,
            ArchivePackageState.CANCELLED,
        }
        if job.terminal:
            return archive_terminal
        return job.state == JobState.PLANNED and not getattr(
            self._settings,
            "publish_enabled",
            False,
        )

    @staticmethod
    def _progress_bar(percent: int) -> str:
        value = max(0, min(100, int(percent)))
        filled = min(10, value // 10)
        return "█" * filled + "░" * (10 - filled)

    @staticmethod
    def _human_bytes(value: int) -> str:
        amount = float(max(0, int(value or 0)))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
            amount /= 1024
        return f"{amount:.1f}TB"

    def _authorized(self, sender_id: int | None) -> bool:
        return sender_id is not None and int(sender_id) in self._settings.allowed_users

    async def _safe_send(self, chat_id: int, text: str, *, buttons=None):
        try:
            return await self._client.send_message(
                chat_id,
                text,
                buttons=buttons,
                parse_mode="md",
            )
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "telegram.status_send.failed",
                "Failed to send status message",
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            return None

    async def _safe_edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        buttons=None,
    ) -> bool:
        try:
            await self._client.edit_message(
                chat_id,
                message_id,
                text,
                buttons=buttons,
                parse_mode="md",
            )
            return True
        except Exception as exc:
            if type(exc).__name__ == "MessageNotModifiedError":
                return True
            log_event(
                self._log,
                logging.WARNING,
                "telegram.status_edit.failed",
                "Failed to edit dynamic job status message",
                exception_type=type(exc).__name__,
            )
            return False

    @staticmethod
    def _from_message(message, chat_id: int) -> IncomingMedia | None:
        if message.photo is not None:
            kind = MediaKind.PHOTO
        elif message.video is not None:
            kind = MediaKind.VIDEO
        elif message.document is not None:
            kind = MediaKind.DOCUMENT
        else:
            return None

        file_info = getattr(message, "file", None)
        original_name = getattr(file_info, "name", None)
        extension = getattr(file_info, "ext", None)
        size = int(getattr(file_info, "size", 0) or 0)
        metadata = {
            "telegram_original_name": original_name,
            "telegram_extension": extension,
        }
        return IncomingMedia(
            kind=kind,
            source=f"telegram:{chat_id}:{message.id}",
            caption=message.message or "",
            size_bytes=size,
            name=original_name,
            spoiler=bool(getattr(message.media, "spoiler", False)),
            grouped_id=message.grouped_id,
            source_chat_id=int(chat_id),
            source_message_id=int(message.id),
            metadata=metadata,
        )

    @staticmethod
    def _from_url_message(message, chat_id: int) -> IncomingMedia | None:
        if getattr(message, "photo", None) is not None or getattr(message, "document", None) is not None:
            return None
        raw = str(getattr(message, "message", "") or "").strip()
        if not raw.lower().startswith(("http://", "https://")):
            return None
        hostname, _port = validate_url_syntax(raw)
        return IncomingMedia(
            kind=MediaKind.DOCUMENT,
            source=f"url:{raw}",
            caption="",
            source_chat_id=int(chat_id),
            source_message_id=int(message.id),
            metadata={
                "source_type": "url",
                "url_hostname": hostname,
            },
        )
