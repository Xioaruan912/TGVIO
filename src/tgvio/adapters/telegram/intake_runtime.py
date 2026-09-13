from __future__ import annotations

from tgvio.adapters.telegram.intake_runtime_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.intake_runtime_support import _PendingBatch
from tgvio.adapters.telegram.intake_status import IntakeStatusMixin
from tgvio.adapters.telegram.intake_collection import IntakeCollectionMixin


class TelethonIntakeRuntime(IntakeStatusMixin, IntakeCollectionMixin):
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
            await self._safe_answer(event, "正在生成预览")
            await self._end_collection(event.chat_id, owner_id)
            return
        if action.startswith("intake:confirm:"):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return
            await self._safe_answer(event, "正在发布合集")
            await self._confirm_collection(event.chat_id, owner_id)
            return
        if action.startswith("intake:abandon:"):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return
            await self._intake.cancel_collection(owner_id=owner_id, chat_id=int(event.chat_id))
            await self._safe_answer(event, "合集已放弃")
            if session.status_message_id is not None:
                await self._safe_edit(
                    int(event.chat_id),
                    int(session.status_message_id),
                    "⛔ **合集已放弃**",
                )
            return
        if action.startswith("intake:prevmode:"):
            session_id = action.split(":", 2)[2]
            session = await self._open_collection(owner_id, event.chat_id)
            if session is None or session.id != session_id:
                await self._safe_answer(event, "合集已经结束或已失效", alert=True)
                return
            preference = await self._intake.get_user_preference(owner_id)
            await self._safe_answer(event, "选择显示模式")
            try:
                await event.edit(
                    self._mode_text(preference.spoiler_mode),
                    buttons=self._mode_buttons(),
                    parse_mode="md",
                )
            except Exception:
                pass
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
            status_message = await self._safe_send(chat_id, await self._accepted_status_text(job))
            status_message_id = getattr(status_message, "id", None)
            if status_message_id is not None:
                await self._save_display_message(job.id, chat_id, int(status_message_id))
        else:
            await self._safe_edit(
                chat_id,
                int(status_message_id),
                await self._accepted_status_text(job),
            )
        self.schedule(
            job,
            chat_id=chat_id,
            status_message_id=(int(status_message_id) if status_message_id is not None else None),
        )

    async def _accepted_order(self, job_id: str) -> int | None:
        repository = self._repository
        if repository is None or not hasattr(repository, "get_accepted_order"):
            return None
        return await repository.get_accepted_order(job_id)

    async def _job_label(self, job_id: str) -> str:
        accepted_order = await self._accepted_order(job_id)
        return f"任务 #{accepted_order}" if accepted_order is not None else "任务"

    async def _accepted_status_text(self, job: Job) -> str:
        label = await self._job_label(job.id)
        return (
            f"✅ 已接收 · **{label}**\n"
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
            message = await self._safe_send(int(chat_id), await self._accepted_status_text(job))
            message_id = getattr(message, "id", None)
            if message_id is not None:
                await self._save_display_message(job.id, int(chat_id), int(message_id))
                ref = JobDisplayMessage(job.id, int(chat_id), int(message_id))
        elif ref is not None and not job.terminal:
            ref = await self._edit_or_replace_display(
                job.id,
                ref,
                await self._accepted_status_text(job),
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
        label = await self._job_label(job.id)
        prompt = (
            f"❓ **{label} 如何显示？**\n\n"
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
                label = await self._job_label(job_id)
                await self._safe_edit(ref.chat_id, ref.message_id, f"⛔ {label} 已取消。")
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
                await self._accepted_status_text(job),
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
                        await self._safe_send(chat_id, f"⛔ {await self._job_label(job.id)} 已取消。")
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
                        await self._safe_send(
                            chat_id,
                            f"❌ {await self._job_label(job.id)} 下载/分析失败。",
                        )
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
                    f"🧠 {await self._job_label(completed.id)} 已完成分析与发布规划。\n"
                    "打开“📋 我的任务”即可查看发布计划。",
                )

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
