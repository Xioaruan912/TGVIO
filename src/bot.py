import asyncio
import logging
import mimetypes
import os
import re
import shutil
from dataclasses import dataclass

from telethon import Button, TelegramClient, events, functions
from telethon.tl import types
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from telethon.utils import get_input_document, get_input_photo

from .config import (
    ALBUM_GATHER_SECONDS,
    ALLOWED_USERS,
    CONFIRM_TIMEOUT,
    DEST_CHANNEL,
    DOWNLOAD_CONCURRENCY,
    DOWNLOAD_DIR,
    DOWNLOAD_TIMEOUT,
    MAX_FILE_SIZE,
    UPLOAD_TIMEOUT,
)
from .downloader import download_video
from .video import guess_mime, is_photo_path, is_video_path, make_thumb, probe_video

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MEDIA_TYPES = (MessageMediaPhoto, MessageMediaDocument)

_CANCELLED = object()

_START_TEXT = (
    "📤 视频转发机器人\n\n"
    f"目标频道: {DEST_CHANNEL}\n\n"
    "使用方式:\n"
    "1. 转发一个含视频/图片的消息给我 → 弹窗确认是否 18+ → 下载后重新上传到频道\n"
    "2. 发送一个链接（抖音/B站/YouTube 等）→ 自动下载并发布到频道\n\n"
    "支持连续发送多个：并行下载、按发送顺序依次上传到频道。\n"
    "一次转发的一批图片（相册）会合并为一个任务、只询问一次，发布为单个相册消息。\n\n"
    "选「是（雪花遮挡）」时，用 Telegram 内置雪花效果遮挡发布，文件内容不被修改。\n\n"
    f"⚠️ 确认弹窗 {CONFIRM_TIMEOUT} 秒内未回复将自动取消该任务。\n"
    f"⚠️ 单个上传文件上限 {MAX_FILE_SIZE // (1024 * 1024)}MB（平台上限）"
)


@dataclass
class _Job:
    seq: int
    kind: str
    status: object
    message: object = None
    album: list = None
    url: str = ""
    spoiler: bool = False


@dataclass
class _PendingJob:
    seq: int
    kind: str
    message: object = None
    album: list = None
    status: object = None
    timeout_task: object = None


@dataclass
class _AlbumBuffer:
    grouped_id: int
    messages: list
    chat_id: int
    task: object = None


class _Pipeline:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.input_q: asyncio.Queue = asyncio.Queue()
        self.jobs: dict[int, _Job] = {}
        self.pending: dict[int, _PendingJob] = {}
        self.albums: dict[int, _AlbumBuffer] = {}
        self.results: dict[int, asyncio.Future] = {}
        self._next_seq = 0
        self._counter = 0
        self._dest_input = None
        self._active_downloads = 0
        self._uploading: int | None = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        for _ in range(max(1, DOWNLOAD_CONCURRENCY)):
            loop.create_task(self._download_worker())
        loop.create_task(self._upload_worker())

    def reserve_seq(self) -> int:
        seq = self._counter
        self._counter += 1
        return seq

    def status_text(self) -> str:
        ready = sum(1 for f in self.results.values() if f.done())
        uploading = f"#{self._uploading}" if self._uploading is not None else "无"
        return (
            "📊 队列状态\n"
            f"▶ 已分配任务: #{self._counter}\n"
            f"⏳ 排队待下载: {self.input_q.qsize()}\n"
            f"⬇️ 正在下载: {self._active_downloads}\n"
            f"📤 正在上传: {uploading}\n"
            f"✅ 已就绪待上传: {ready}\n"
            f"❓ 等待 18+ 确认: {len(self.pending)}\n"
            f"🖼 相册聚合中: {len(self.albums)}"
        )

    def submit(self, kind: str, status: object, message: object, url: str = "") -> int:
        seq = self.reserve_seq()
        self.enqueue(_Job(seq=seq, kind=kind, status=status, message=message, url=url))
        return seq

    def enqueue(self, job: _Job) -> None:
        self.input_q.put_nowait(job)

    def register_pending(
        self, seq: int, kind: str, message: object = None, album: list = None
    ) -> None:
        self.pending[seq] = _PendingJob(
            seq=seq, kind=kind, message=message, album=album
        )
        self.pending[seq].timeout_task = asyncio.get_running_loop().create_task(
            self._confirm_timeout(seq)
        )

    def set_pending_status(self, seq: int, status: object) -> None:
        if seq in self.pending:
            self.pending[seq].status = status

    def collect_album(self, grouped_id: int, message: object, chat_id: int) -> None:
        buf = self.albums.get(grouped_id)
        if buf is None:
            buf = _AlbumBuffer(grouped_id=grouped_id, messages=[], chat_id=chat_id)
            self.albums[grouped_id] = buf
        if not any(m.id == message.id for m in buf.messages):
            buf.messages.append(message)
        if buf.task is not None:
            buf.task.cancel()
        buf.task = asyncio.get_running_loop().create_task(self._finalize_album(buf))

    async def _finalize_album(self, buf: _AlbumBuffer) -> None:
        await asyncio.sleep(ALBUM_GATHER_SECONDS)
        self.albums.pop(buf.grouped_id, None)
        seq = self.reserve_seq()
        self.register_pending(seq, "album", album=list(buf.messages))
        try:
            status = await self.client.send_message(
                buf.chat_id,
                f"⚠️ #{seq} 该相册（{len(buf.messages)} 张）是否为 18+？",
                buttons=[
                    Button.inline("🔞 是（雪花遮挡）", f"confirm:{seq}:1"),
                    Button.inline("✅ 否", f"confirm:{seq}:0"),
                ],
            )
            self.set_pending_status(seq, status)
        except Exception as exc:
            logger.exception("Failed to ask album confirmation #%s", seq)
            self.pending.pop(seq, None)
            self._set_cancelled(seq)

    async def _confirm_timeout(self, seq: int) -> None:
        await asyncio.sleep(CONFIRM_TIMEOUT)
        pending = self.pending.pop(seq, None)
        if pending is None:
            return
        self._set_cancelled(seq)
        logger.info("Job #%s cancelled by confirmation timeout", seq)
        if pending.status is not None:
            try:
                await pending.status.edit(f"⏰ #{seq} 确认超时，任务已取消")
            except Exception:
                pass

    async def _download_worker(self) -> None:
        while True:
            job = await self.input_q.get()
            self.jobs[job.seq] = job
            self._active_downloads += 1
            try:
                path = await asyncio.wait_for(
                    self._do_download(job), timeout=DOWNLOAD_TIMEOUT
                )
                self._set_result(job.seq, path)
            except asyncio.TimeoutError:
                logger.error("Job #%s timed out after %ss", job.seq, DOWNLOAD_TIMEOUT)
                self._set_exception(
                    job.seq, TimeoutError(f"下载超时（{DOWNLOAD_TIMEOUT} 秒）")
                )
            except Exception as exc:
                logger.exception("Download failed for job #%s", job.seq)
                self._set_exception(job.seq, exc)
            finally:
                self._active_downloads = max(0, self._active_downloads - 1)
                self.input_q.task_done()

    def _progress_logger(self, seq: int):
        last = {"tenth": -1}

        async def progress(received: int, total: int) -> None:
            tenth = int(received * 10 / total) if total else 0
            if tenth != last["tenth"]:
                last["tenth"] = tenth
                logger.info(
                    "Job #%s download progress: %d/%d (%d%%)",
                    seq,
                    received,
                    total,
                    (received * 100 // total) if total else 0,
                )

        return progress

    async def _do_download(self, job: _Job):
        workdir = self._workdir(job.seq)
        os.makedirs(workdir, exist_ok=True)
        await job.status.edit(f"🔄 #{job.seq} 正在下载...")
        progress = self._progress_logger(job.seq)
        if job.kind == "album":
            paths = []
            for message in job.album:
                logger.info("Job #%s downloading album item %s", job.seq, message.id)
                path = await message.download_media(
                    file=workdir, progress_callback=progress
                )
                if not path:
                    raise RuntimeError("未能下载相册媒体文件")
                paths.append(path)
            return paths
        if job.kind == "media":
            path = await job.message.download_media(
                file=workdir, progress_callback=progress
            )
        else:
            path, _ = await download_video(job.url, workdir)
        if not path:
            raise RuntimeError("未能下载媒体文件")
        return path

    async def _upload_worker(self) -> None:
        watchdog = DOWNLOAD_TIMEOUT + 60
        while True:
            seq = self._next_seq
            fut = self.results.get(seq)
            if fut is None:
                fut = asyncio.get_running_loop().create_future()
                self.results[seq] = fut
            try:
                path = await asyncio.wait_for(
                    asyncio.shield(fut), timeout=watchdog
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Job #%s unresolved for %ss, forcing cancel", seq, watchdog
                )
                self._set_cancelled(seq)
            except Exception as exc:
                await self._reply_error(seq, f"下载失败: {exc}")
            else:
                if path is _CANCELLED:
                    logger.info("Job #%s skipped (cancelled)", seq)
                else:
                    self._uploading = seq
                    try:
                        await asyncio.wait_for(
                            self._publish(seq, path), timeout=UPLOAD_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            "Upload for job #%s timed out after %ss",
                            seq,
                            UPLOAD_TIMEOUT,
                        )
                        await self._reply_error(
                            seq, f"上传超时（{UPLOAD_TIMEOUT} 秒）"
                        )
                    except Exception as exc:
                        logger.exception("Upload failed for job #%s", seq)
                        await self._reply_error(seq, f"上传失败: {exc}")
                    finally:
                        self._uploading = None
            finally:
                self._next_seq += 1
                self.results.pop(seq, None)
                self.jobs.pop(seq, None)
                shutil.rmtree(self._workdir(seq), ignore_errors=True)

    async def _publish(self, seq: int, payload) -> None:
        job = self.jobs[seq]
        if isinstance(payload, list):
            await self._publish_album(seq, payload)
            return

        path = payload
        size = os.path.getsize(path)
        if size > MAX_FILE_SIZE:
            await job.status.edit(
                f"❌ #{seq} 文件 {size / 1024 / 1024:.1f}MB 超过 "
                f"{MAX_FILE_SIZE // (1024 * 1024)}MB 上限"
            )
            return

        waiting = sum(1 for s, f in self.results.items() if s != seq and f.done())
        suffix = f"（另有 {waiting} 个待上传）" if waiting else ""
        label = "🔞 雪花遮挡" if job.spoiler else ""
        await job.status.edit(
            f"✅ #{seq} 下载完成（{size / 1024 / 1024:.1f}MB）{label}，正在上传{suffix}..."
        )

        caption = None
        if job.kind == "media":
            caption = job.message.message[:1024] or None
        await self._send_media(path, caption, job.spoiler, job.seq)
        await job.status.edit(f"✅ #{seq} 已发布到 {DEST_CHANNEL}")

    async def _publish_album(self, seq: int, paths: list) -> None:
        job = self.jobs[seq]
        for path in paths:
            size = os.path.getsize(path)
            if size > MAX_FILE_SIZE:
                await job.status.edit(
                    f"❌ #{seq} 相册中有文件 {size / 1024 / 1024:.1f}MB 超过 "
                    f"{MAX_FILE_SIZE // (1024 * 1024)}MB 上限"
                )
                return

        total = sum(os.path.getsize(p) for p in paths)
        waiting = sum(1 for s, f in self.results.items() if s != seq and f.done())
        suffix = f"（另有 {waiting} 个待上传）" if waiting else ""
        label = "🔞 雪花遮挡" if job.spoiler else ""
        await job.status.edit(
            f"✅ #{seq} 相册下载完成（{len(paths)} 张，共 {total / 1024 / 1024:.1f}MB）"
            f"{label}，正在上传{suffix}..."
        )

        captions = [m.message[:1024] or "" for m in job.album]
        await self._send_album(paths, captions, job.spoiler, seq)
        await job.status.edit(f"✅ #{seq} 相册已发布到 {DEST_CHANNEL}")

    async def _send_media(self, path: str, caption: str | None, spoiler: bool, seq: int) -> None:
        media = await self._upload_media_input(path, spoiler, seq)
        await self.client.send_file(DEST_CHANNEL, media, caption=caption)

    async def _upload_media_input(self, path: str, spoiler: bool, seq: int):
        uploaded = await self.client.upload_file(path)

        if is_photo_path(path):
            return types.InputMediaUploadedPhoto(
                file=uploaded, spoiler=spoiler or None
            )

        os.makedirs(self._workdir(seq), exist_ok=True)
        attributes = [
            types.DocumentAttributeFilename(file_name=os.path.basename(path))
        ]
        mime = guess_mime(path)
        thumb_input = None
        nosound = None

        if is_video_path(path):
            duration, width, height = 0, 1, 1
            try:
                duration, width, height = await probe_video(path)
            except Exception as exc:
                logger.warning("Video probe failed for job #%s: %s", seq, exc)
            try:
                thumb = await make_thumb(path, self._workdir(seq))
                if thumb:
                    thumb_input = await self.client.upload_file(thumb)
            except Exception as exc:
                logger.warning("Video thumb failed for job #%s: %s", seq, exc)
            attributes.insert(
                0,
                types.DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True,
                ),
            )
            nosound = True

        return types.InputMediaUploadedDocument(
            file=uploaded,
            mime_type=mime,
            attributes=attributes,
            thumb=thumb_input,
            spoiler=spoiler or None,
            nosound_video=nosound,
        )

    async def _send_album(
        self, paths: list, captions: list, spoiler: bool, seq: int
    ) -> None:
        dest = await self._get_dest_input()
        single_media = []
        for index, path in enumerate(paths):
            fm = await self._upload_media_input(path, spoiler, seq)

            result = await self.client(
                functions.messages.UploadMediaRequest(dest, fm)
            )
            if isinstance(result, types.MessageMediaPhoto):
                reference = types.InputMediaPhoto(
                    id=get_input_photo(result.photo), spoiler=spoiler or None
                )
            elif isinstance(result, types.MessageMediaDocument):
                reference = types.InputMediaDocument(
                    id=get_input_document(result.document),
                    spoiler=spoiler or None,
                )
            else:
                raise RuntimeError(
                    f"无法为相册媒体 #{(index + 1)} 构建引用"
                )

            caption = captions[index] if index < len(captions) else ""
            single_media.append(
                types.InputSingleMedia(reference, message=caption)
            )

        await self.client(
            functions.messages.SendMultiMediaRequest(
                dest, multi_media=single_media
            )
        )

    async def _get_dest_input(self):
        if self._dest_input is None:
            self._dest_input = await self.client.get_input_entity(DEST_CHANNEL)
        return self._dest_input

    def _workdir(self, seq: int) -> str:
        return os.path.join(DOWNLOAD_DIR, f"job-{seq}")

    def _set_result(self, seq: int, value: str) -> None:
        fut = self.results.get(seq)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self.results[seq] = fut
        if not fut.done():
            fut.set_result(value)

    def _set_exception(self, seq: int, exc: Exception) -> None:
        fut = self.results.get(seq)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self.results[seq] = fut
        if not fut.done():
            fut.set_exception(exc)

    def _set_cancelled(self, seq: int) -> None:
        fut = self.results.get(seq)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self.results[seq] = fut
        if not fut.done():
            fut.set_result(_CANCELLED)

    async def _reply_error(self, seq: int, text: str) -> None:
        job = self.jobs.get(seq)
        if job is not None:
            try:
                await job.status.edit(f"❌ #{seq} {text}")
            except Exception:
                pass


def register_handlers(client: TelegramClient) -> None:
    pipeline = _Pipeline(client)
    pipeline.start()

    def _authorized(event: events.NewMessage.Event) -> bool:
        return event.sender_id in ALLOWED_USERS

    @client.on(events.NewMessage(pattern="/start"))
    async def on_start(event: events.NewMessage.Event) -> None:
        if not _authorized(event):
            return
        await event.respond(_START_TEXT)

    @client.on(events.NewMessage(pattern="/status"))
    async def on_status(event: events.NewMessage.Event) -> None:
        if not _authorized(event):
            return
        await event.respond(pipeline.status_text())

    @client.on(events.CallbackQuery())
    async def on_callback(event: events.CallbackQuery.Event) -> None:
        async def _answer(text: str = "") -> None:
            try:
                await event.answer(text)
            except Exception:
                pass

        if event.sender_id not in ALLOWED_USERS:
            await _answer("无权限")
            return
        try:
            _, seq_str, flag_str = event.data.decode().split(":")
            seq, spoiler = int(seq_str), bool(int(flag_str))
        except (ValueError, IndexError):
            await _answer("无效操作")
            return

        pending = pipeline.pending.pop(seq, None)
        if pending is None:
            await _answer("该确认已失效")
            return
        if pending.timeout_task is not None:
            pending.timeout_task.cancel()

        label = "🔞 雪花遮挡" if spoiler else "✅ 正常"
        try:
            new_status = await event.client.send_message(
                event.chat_id,
                f"🔄 #{seq} 已确认：{label}，前方等待 "
                f"{max(0, pipeline.input_q.qsize())} 个...",
            )
        except Exception:
            new_status = pending.status
        else:
            try:
                await event.delete()
            except Exception:
                pass

        try:
            pipeline.enqueue(
                _Job(
                    seq=seq,
                    kind=pending.kind,
                    status=new_status,
                    message=pending.message,
                    album=pending.album,
                    spoiler=spoiler,
                )
            )
        except Exception:
            logger.exception("Enqueue failed for job #%s", seq)
            pipeline._set_cancelled(seq)
        else:
            logger.info("Job #%s confirmed spoiler=%s", seq, spoiler)
        await _answer("已确认")

    @client.on(events.NewMessage(func=lambda e: e.is_private))
    async def on_private_message(event: events.NewMessage.Event) -> None:
        logger.info(
            "NewMessage from %s media=%s grouped=%s text_len=%s",
            event.sender_id,
            type(event.message.media).__name__,
            event.message.grouped_id,
            len(event.raw_text or ""),
        )
        if not _authorized(event):
            logger.info("Ignoring unauthorized user %s", event.sender_id)
            return

        if isinstance(event.message.media, MEDIA_TYPES):
            grouped_id = event.message.grouped_id
            if grouped_id:
                logger.info("Album message -> collect_album gid=%s", grouped_id)
                pipeline.collect_album(grouped_id, event.message, event.chat_id)
                return
            seq = pipeline.reserve_seq()
            pipeline.register_pending(seq, "media", event.message)
            logger.info("Sending 18+ question for #%s", seq)
            try:
                status = await event.reply(
                    f"⚠️ #{seq} 该内容是否为 18+？",
                    buttons=[
                        Button.inline("🔞 是（雪花遮挡）", f"confirm:{seq}:1"),
                        Button.inline("✅ 否", f"confirm:{seq}:0"),
                    ],
                )
                logger.info("18+ question sent for #%s (msg id=%s)", seq, status.id)
            except Exception as exc:
                logger.exception("18+ question FAILED for #%s", seq)
                await event.respond(f"发送确认失败: {exc}")
            pipeline.set_pending_status(seq, status)
            return

        url_match = URL_RE.search(event.raw_text or "")
        if url_match:
            status = await event.reply("⏳ 正在加入队列...")
            seq = pipeline.submit("url", status, event.message, url_match.group(0))
            await status.edit(
                f"⏳ #{seq} 已加入队列（前方等待 {max(0, pipeline.input_q.qsize() - 1)} 个）"
            )
            return

        await event.respond("请发送视频或链接，或使用 /start 查看使用说明。")
