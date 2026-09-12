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
from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.job_control import JobCancelRequested
from tgvio.application.job_runner import JobRunner
from tgvio.config import Settings
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.job import Job, JobState, MediaKind
from tgvio.domain.progress import JobProgress
from tgvio.infrastructure.url_security import validate_url_syntax
from tgvio.observability import log_event


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
        self._status_tasks: set[asyncio.Task] = set()
        self._pending_batches: dict[tuple[int, int], _PendingBatch] = {}
        self._flush_tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(settings.worker_concurrency)

    def register(self) -> None:
        self._client.add_event_handler(self._on_album, events.Album())
        self._client.add_event_handler(
            self._on_message,
            events.NewMessage(incoming=True, func=lambda event: event.message.grouped_id is None),
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
        if self._status_tasks:
            status_tasks = tuple(self._status_tasks)
            for task in status_tasks:
                task.cancel()
            await asyncio.gather(*status_tasks, return_exceptions=True)

    async def _on_message(self, event) -> None:
        if not self._authorized(event.sender_id):
            return
        if (event.raw_text or "").lstrip().startswith("/"):
            return
        incoming = self._from_message(event.message, event.chat_id)
        if incoming is None and self._settings.url_enabled:
            try:
                incoming = self._from_url_message(event.message, event.chat_id)
            except ValueError:
                await self._safe_send(event.chat_id, "🛡️ URL 已拒绝：不符合 TGVIO 安全策略。")
                return
        if incoming is None:
            return
        await self._queue_batch(event.chat_id, event.sender_id, [incoming])

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
        if media:
            await self._queue_batch(event.chat_id, sender_id, media)

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
        job = await self._intake.accept(
            owner_id=int(sender_id),
            destination=self._settings.destination,
            media=batch,
        )
        log_event(
            self._log,
            logging.INFO,
            "intake.job.accepted",
            job_id=job.id,
            item_count=len(job.items),
            input_kinds=",".join(sorted({item.kind.value for item in job.items})),
        )
        status_message = await self._safe_send(
            chat_id,
            f"✅ 已接收任务 `{job.id[:10]}`\n媒体：{len(job.items)}\n正在下载和分析。",
        )
        self.schedule(
            job,
            chat_id=chat_id,
            status_message_id=getattr(status_message, "id", None),
        )

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
            tracker = asyncio.create_task(
                self._track_status(int(chat_id), int(status_message_id), job.id),
                name=f"tgvio-status-{job.id}",
            )
            self._status_tasks.add(tracker)
            tracker.add_done_callback(self._status_tasks.discard)
        task = asyncio.create_task(
            self._process_job(chat_id, job, status_message_id=status_message_id),
            name=f"tgvio-job-{job.id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_job(
        self,
        chat_id: int | None,
        job: Job,
        *,
        status_message_id: int | None = None,
    ) -> None:
        async with self._semaphore:
            try:
                completed = await self._processor.process(job)
            except asyncio.CancelledError:
                raise
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
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "job.run.failed",
                    "Job failed during intake pipeline",
                    job_id=job.id,
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )
                if chat_id is not None and status_message_id is None:
                    await self._safe_send(chat_id, f"❌ 任务 `{job.id[:10]}` 下载/分析失败。")
                return
            log_event(
                self._log,
                logging.INFO,
                "job.run.result",
                job_id=completed.id,
                state=completed.state.value,
            )
            if chat_id is not None and status_message_id is None:
                if completed.state == JobState.SUCCEEDED:
                    text = f"✅ 任务 `{completed.id[:10]}` 已发布完成。"
                else:
                    text = (
                        f"🧠 任务 `{completed.id[:10]}` 已完成分析与发布规划。\n"
                        "打开“📋 我的任务”即可查看发布计划。"
                    )
                await self._safe_send(chat_id, text)

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

                text = self._render_live_status(job, progress, archive, speed_bps=speed_bps)
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
    ) -> str:
        total_bytes = sum(max(0, int(item.size_bytes or 0)) for item in job.items)
        lines = [
            f"✅ 已接收任务 `{job.id[:10]}`",
            f"媒体：`{len(job.items)}` · `{self._human_bytes(total_bytes)}`",
        ]

        phase = progress.phase if progress is not None else job.state.value
        if phase == "downloading" and progress is not None:
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
        ):
            rows[0].append(
                Button.inline(
                    "🔁 重试任务",
                    f"ui:retry:{job.id}".encode("utf-8"),
                )
            )
        if archive is not None and archive.state == ArchivePackageState.FAILED:
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

    async def _safe_send(self, chat_id: int, text: str):
        try:
            return await self._client.send_message(chat_id, text, parse_mode="md")
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
