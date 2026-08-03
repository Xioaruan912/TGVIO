import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import time
from dataclasses import dataclass

from telethon import Button, TelegramClient, events, functions
from telethon.tl import types
from telethon.tl.types import (
    MessageEntityBotCommand,
    MessageMediaDocument,
    MessageMediaPhoto,
)
from telethon.utils import get_input_document, get_input_photo

from .config import (
    ALBUM_GATHER_SECONDS,
    ALLOWED_USERS,
    AUTO_DELETE_SECONDS,
    CONFIRM_TIMEOUT,
    DEST_CHANNEL,
    DOWNLOAD_CONCURRENCY,
    DOWNLOAD_DIR,
    DOWNLOAD_TIMEOUT,
    FORWARD_CAPTION,
    HELD_TIMEOUT,
    MAX_FILE_SIZE,
    PROGRESS_MIN_INTERVAL,
    UPLOAD_TIMEOUT,
)
from .downloader import download_video
from .video import guess_mime, is_photo_path, is_video_path, make_thumb, probe_video

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MEDIA_TYPES = (MessageMediaPhoto, MessageMediaDocument)

_CANCELLED = object()

PREFS_FILE = os.path.join("session", "prefs.json")
LEGACY_PREFS_FILE = os.path.join("session", "progress_prefs.json")
PROGRESS_REFRESH_SECONDS = 1.0

_MODE_NAMES = {
    "ask": "每次询问",
    "always_spoiler": "总是雪花遮挡",
    "always_normal": "总是正常",
}


def _mode_buttons():
    return [
        [Button.inline("🟡 每次询问", "mode:ask")],
        [Button.inline("🔞 总是雪花遮挡", "mode:always_spoiler")],
        [Button.inline("✅ 总是正常", "mode:always_normal")],
    ]


def render_bar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, round(pct * width / 100)))
    return "█" * filled + "░" * (width - filled)


async def _delete_after(message: object, seconds: float) -> None:
    if not seconds or seconds <= 0:
        return
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception:
        pass

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
    f"⚠️ 单个上传文件上限 {MAX_FILE_SIZE // (1024 * 1024)}MB（平台上限）\n\n"
    "ℹ️ 关于：视频转发机器人，把转发内容处理后发布到频道。输入 /about 查看全部命令说明。"
)

_ABOUT_TEXT = (
    "ℹ️ 关于 · 视频转发机器人\n\n"
    f"把转发的视频/图片/链接处理后发布到 {DEST_CHANNEL}。\n"
    "支持 18+ 雪花遮挡、相册聚合、并行下载/顺序上传队列、\n"
    "进度条、撤销发布与失败重试。\n\n"
    "📖 命令说明：\n"
    "/start    使用说明（首次运行设置 18+ 模式）\n"
    "/about    关于/命令说明\n"
    "/status   查看队列全貌（排位/阶段/进度）\n"
    "/progress 查看所有任务的下载/上传进度条\n"
    "/mode     设置 18+ 处理方式（每次询问/总是雪花/总是正常）\n"
    "/queue    管理队列（逐项取消/暂停/恢复）\n"
    "/cancel N 取消第 N 个待确认项\n"
    "/pause    暂停队列\n"
    "/resume   恢复队列"
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
    user_id: int = 0


@dataclass
class _PendingJob:
    seq: int
    kind: str
    message: object = None
    album: list = None
    status: object = None
    timeout_task: object = None
    user_id: int = 0


@dataclass
class _AlbumBuffer:
    grouped_id: int
    messages: list
    chat_id: int
    task: object = None


@dataclass
class _HeldItem:
    kind: str
    message: object = None
    album: list = None
    chat_id: int = 0


class _Pipeline:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.input_q: asyncio.Queue = asyncio.Queue()
        self.jobs: dict[int, _Job] = {}
        self.pending: dict[int, _PendingJob] = {}
        self.albums: dict[int, _AlbumBuffer] = {}
        self.results: dict[int, asyncio.Future] = {}
        self.active: dict[int, dict] = {}
        self.active_seqs: set[int] = set()
        self._next_seq = 0
        self._counter = 0
        self._dest_input = None
        self._active_downloads = 0
        self._uploading: int | None = None
        self._download_tasks: dict[int, asyncio.Task] = {}
        self._upload_tasks: dict[int, asyncio.Task] = {}
        self.prefs: dict[int, dict] = {}
        self.published: dict[int, list] = {}
        self.retryable: dict[int, _Job] = {}
        self._paused = False
        self._last_progress_edit = 0.0
        self._cancel_marked: set[int] = set()
        self.held: dict[int, list[_HeldItem]] = {}
        self._held_timers: dict[int, asyncio.Task] = {}
        self._load_prefs()

    def _load_prefs(self) -> None:
        try:
            with open(PREFS_FILE) as f:
                data = json.load(f)
            self.prefs = {int(k): dict(v) for k, v in data.items()}
        except Exception:
            self.prefs = {}
        if not self.prefs:
            try:
                with open(LEGACY_PREFS_FILE) as f:
                    legacy = json.load(f)
                for uid, show in legacy.items():
                    self.prefs.setdefault(int(uid), {})["show_progress"] = bool(show)
                if self.prefs:
                    self._save_prefs()
            except Exception:
                pass

    def _save_prefs(self) -> None:
        try:
            with open(PREFS_FILE, "w") as f:
                json.dump(
                    {str(k): v for k, v in self.prefs.items()}, f, ensure_ascii=False
                )
        except Exception:
            pass

    def _get_pref(self, user_id: int, key: str, default):
        return self.prefs.get(user_id, {}).get(key, default)

    def _has_pref(self, user_id: int, key: str) -> bool:
        return key in self.prefs.get(user_id, {})

    def _set_pref(self, user_id: int, key: str, value) -> None:
        self.prefs.setdefault(user_id, {})[key] = value
        self._save_prefs()

    def _show_progress(self, user_id: int) -> bool:
        return self._get_pref(user_id, "show_progress", True)

    def toggle_progress_pref(self, user_id: int) -> bool:
        current = self._show_progress(user_id)
        self._set_pref(user_id, "show_progress", not current)
        return not current

    def _spoiler_mode(self, user_id: int) -> str:
        return self._get_pref(user_id, "spoiler_mode", "ask")

    def set_spoiler_mode(self, user_id: int, mode: str) -> None:
        self._set_pref(user_id, "spoiler_mode", mode)

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        for _ in range(max(1, DOWNLOAD_CONCURRENCY)):
            loop.create_task(self._download_worker())
        loop.create_task(self._upload_worker())

    def reserve_seq(self) -> int:
        seq = self._counter
        self._counter += 1
        return seq

    def status_text(self, user_id: int = 0) -> str:
        show = self._show_progress(user_id) if user_id else True
        lines = [
            "📊 队列状态" + ("（⏸ 已暂停）" if self._paused else "")
        ]
        active_lines = []
        for seq in sorted(self.active_seqs):
            pos = self.task_label(seq)
            info = self.active.get(seq)
            if seq in self._download_tasks:
                if info and show:
                    prefix = (
                        f"⬇ 下载 {info['item']}/{info['items']}"
                        if info["items"] > 1
                        else "🔄 正在下载"
                    )
                    active_lines.append(
                        f"{pos} {prefix} {render_bar(info['pct'])} "
                        f"{info['pct']:3d}%"
                    )
                else:
                    active_lines.append(f"{pos} 🔄 正在下载")
            elif seq == self._uploading:
                if info and show:
                    prefix = (
                        f"📤 上传 {info['item']}/{info['items']}"
                        if info["items"] > 1
                        else "📤 正在上传"
                    )
                    active_lines.append(
                        f"{pos} {prefix} {render_bar(info['pct'])} "
                        f"{info['pct']:3d}%"
                    )
                else:
                    active_lines.append(f"{pos} 📤 正在上传")
            elif seq in self.jobs:
                active_lines.append(f"{pos} ✅ 等待上传")
            else:
                active_lines.append(f"{pos} ⏳ 等待下载")

        if active_lines:
            lines.append(f"\n▶ 进行中（{len(active_lines)}）")
            lines.extend(active_lines)
        else:
            lines.append("\n▶ 进行中：无")

        extras = []
        if self.pending:
            extras.append(f"❓ 等待确认: {len(self.pending)}")
        if self.albums:
            extras.append(f"🖼 相册聚合中: {len(self.albums)}")
        if extras:
            lines.append("\n▶ 其他")
            lines.extend(extras)

        return "\n".join(lines)

    def submit(
        self, kind: str, status: object, message: object, url: str = "", user_id: int = 0
    ) -> int:
        seq = self.reserve_seq()
        self.enqueue(
            _Job(
                seq=seq,
                kind=kind,
                status=status,
                message=message,
                url=url,
                user_id=user_id,
            )
        )
        return seq

    def enqueue(self, job: _Job) -> None:
        self.input_q.put_nowait(job)
        self.active_seqs.add(job.seq)

    def _queue_position(self, seq: int) -> int:
        return 1 + sum(1 for s in self.active_seqs if s < seq)

    def task_label(self, seq: int) -> str:
        return f"队列第 {self._queue_position(seq)} 位"

    def register_pending(
        self,
        seq: int,
        kind: str,
        message: object = None,
        album: list = None,
        user_id: int = 0,
    ) -> None:
        self.pending[seq] = _PendingJob(
            seq=seq, kind=kind, message=message, album=album, user_id=user_id
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
        if not self._has_pref(buf.chat_id, "spoiler_mode"):
            first = self.hold_item(
                buf.chat_id, "album", album=list(buf.messages), chat_id=buf.chat_id
            )
            try:
                if first:
                    await self.client.send_message(
                        buf.chat_id,
                        "📌 请先设置 18+ 处理方式，再继续处理相册：",
                        buttons=_mode_buttons(),
                    )
                else:
                    msg = await self.client.send_message(
                        buf.chat_id, "⏳ 已暂存，等待你设置 18+ 模式"
                    )
                    if AUTO_DELETE_SECONDS > 0:
                        asyncio.get_running_loop().create_task(
                            _delete_after(msg, AUTO_DELETE_SECONDS)
                        )
            except Exception as exc:
                logger.exception("Failed to prompt mode for album: %s", exc)
            return
        if self._spoiler_mode(buf.chat_id) != "ask":
            try:
                await self._auto_enqueue(
                    "album", None, list(buf.messages), buf.chat_id
                )
            except Exception as exc:
                logger.exception("Failed to auto-enqueue album: %s", exc)
            return
        seq = self.reserve_seq()
        try:
            await self._show_ask(
                seq, "album", None, list(buf.messages), buf.chat_id, buf.chat_id
            )
        except Exception as exc:
            logger.exception("Failed to ask album confirmation #%s: %s", seq, exc)

    async def _confirm_timeout(self, seq: int) -> None:
        await asyncio.sleep(CONFIRM_TIMEOUT)
        pending = self.pending.pop(seq, None)
        if pending is None:
            return
        self._set_cancelled(seq)
        logger.info("Job #%s cancelled by confirmation timeout", seq)
        if pending.status is not None:
            try:
                await pending.status.delete()
            except Exception:
                pass

    async def _download_worker(self) -> None:
        while True:
            while self._paused:
                await asyncio.sleep(1)
            job = await self.input_q.get()
            if job.seq in self._cancel_marked:
                self._cancel_marked.discard(job.seq)
                await self._delete_status(job)
                self.input_q.task_done()
                continue
            self.jobs[job.seq] = job
            self._active_downloads += 1
            task = asyncio.get_running_loop().create_task(self._do_download(job))
            self._download_tasks[job.seq] = task
            try:
                path = await asyncio.wait_for(task, timeout=DOWNLOAD_TIMEOUT)
                self._set_result(job.seq, path)
            except asyncio.TimeoutError:
                logger.error("Job #%s timed out after %ss", job.seq, DOWNLOAD_TIMEOUT)
                self._set_exception(
                    job.seq, TimeoutError(f"下载超时（{DOWNLOAD_TIMEOUT} 秒）")
                )
            except asyncio.CancelledError:
                logger.info("Job #%s download stopped by user", job.seq)
                self._cancel_marked.discard(job.seq)
                self._set_cancelled(job.seq)
                await self._delete_status(job)
            except Exception as exc:
                logger.exception("Download failed for job #%s", job.seq)
                self._set_exception(job.seq, exc)
            finally:
                self._download_tasks.pop(job.seq, None)
                self._active_downloads = max(0, self._active_downloads - 1)
                self.input_q.task_done()

    def _make_download_progress(
        self, job: _Job, item: int = 1, items: int = 1
    ):
        last = {"pct": -1, "edit": 0.0}

        async def progress(received: int, total: int) -> None:
            pct = int(received * 100 / total) if total else 0
            if pct == last["pct"]:
                return
            last["pct"] = pct
            if items <= 1:
                overall = pct
            else:
                frac = received / total if total else 0
                overall = round(((item - 1) + frac) * 100 / items)
            self.active[job.seq] = {
                "phase": "download",
                "pct": overall,
                "item": item,
                "items": items,
                "user_id": job.user_id,
            }
            now = time.time()
            if pct == 100 or now - last["edit"] >= PROGRESS_REFRESH_SECONDS:
                last["edit"] = now
                logger.info(
                    "Job #%s download progress: %d/%d (%d%%)",
                    job.seq,
                    received,
                    total,
                    pct,
                )
                await self._update_progress_status(job.seq)

        return progress

    def _make_upload_progress(self, seq: int, item: int = 1, items: int = 1):
        last = {"pct": -1, "edit": 0.0}
        job = self.jobs.get(seq)
        user_id = job.user_id if job is not None else 0

        async def progress(received: int, total: int) -> None:
            pct = int(received * 100 / total) if total else 0
            if pct == last["pct"]:
                return
            last["pct"] = pct
            if items <= 1:
                overall = pct
            else:
                frac = received / total if total else 0
                overall = round(((item - 1) + frac) * 100 / items)
            self.active[seq] = {
                "phase": "upload",
                "pct": overall,
                "item": item,
                "items": items,
                "user_id": user_id,
            }
            now = time.time()
            if pct == 100 or now - last["edit"] >= PROGRESS_REFRESH_SECONDS:
                last["edit"] = now
                await self._update_progress_status(seq)

        return progress

    async def _update_progress_status(self, seq: int) -> None:
        now = time.time()
        if now - self._last_progress_edit < PROGRESS_MIN_INTERVAL:
            return
        self._last_progress_edit = now
        job = self.jobs.get(seq)
        info = self.active.get(seq)
        if job is None or info is None:
            return
        show = self._show_progress(info["user_id"])
        phase, item, items, pct = (
            info["phase"],
            info["item"],
            info["items"],
            info["pct"],
        )
        if items > 1:
            prefix = (
                f"⬇ {self.task_label(seq)} 下载 {item}/{items}"
                if phase == "download"
                else f"📤 {self.task_label(seq)} 上传 {item}/{items}"
            )
        else:
            prefix = (
                f"🔄 {self.task_label(seq)} 正在下载"
                if phase == "download"
                else f"📤 {self.task_label(seq)} 正在上传"
            )
        if show:
            text = f"{prefix} {render_bar(pct)} {pct:3d}%"
        else:
            text = f"{prefix}..."
        toggle = (
            Button.inline("🔕 关闭进度", "toggle_progress")
            if show
            else Button.inline("🔔 显示进度", "toggle_progress")
        )
        buttons = [toggle]
        if phase == "download":
            buttons.append(Button.inline("⏹ 停止下载", f"stop:{seq}"))
        elif phase == "upload":
            buttons.append(Button.inline("⏹ 停止上传", f"stop:{seq}"))
        try:
            await job.status.edit(text, buttons=buttons)
        except Exception:
            pass

    async def _safe_edit(self, job, text: str, buttons=None) -> None:
        if job is None:
            return
        try:
            await job.status.edit(text, buttons=buttons)
        except Exception:
            pass

    async def _delete_status(self, job) -> None:
        if job is None:
            return
        try:
            await job.status.delete()
        except Exception:
            pass

    async def _do_download(self, job: _Job):
        workdir = self._workdir(job.seq)
        os.makedirs(workdir, exist_ok=True)
        await self._safe_edit(job, f"🔄 {self.task_label(job.seq)} 正在下载...")
        if job.kind == "album":
            paths = []
            total = len(job.album)
            for index, message in enumerate(job.album, start=1):
                logger.info("Job #%s downloading album item %s", job.seq, message.id)
                progress = self._make_download_progress(
                    job, item=index, items=total
                )
                path = await message.download_media(
                    file=workdir, progress_callback=progress
                )
                if not path:
                    raise RuntimeError("未能下载相册媒体文件")
                paths.append(path)
            return paths
        if job.kind == "media":
            progress = self._make_download_progress(job)
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
            while self._paused:
                await asyncio.sleep(1)
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
                if self._paused:
                    continue
                logger.error(
                    "Job #%s unresolved for %ss, forcing cancel", seq, watchdog
                )
                await self._reply_error(
                    seq,
                    f"处理超时（{watchdog} 秒）",
                    retry_job=self.jobs.get(seq),
                )
                self._set_cancelled(seq)
            except Exception as exc:
                await self._reply_error(
                    seq, f"下载失败: {exc}", retry_job=self.jobs.get(seq)
                )
            else:
                if path is _CANCELLED:
                    logger.info("Job #%s skipped (cancelled)", seq)
                elif seq in self._cancel_marked:
                    logger.info("Job #%s cancelled before upload", seq)
                    self._cancel_marked.discard(seq)
                    await self._delete_status(self.jobs.get(seq))
                else:
                    while self._paused:
                        await asyncio.sleep(1)
                    self._uploading = seq
                    task = asyncio.get_running_loop().create_task(
                        self._publish(seq, path)
                    )
                    self._upload_tasks[seq] = task
                    try:
                        await asyncio.wait_for(task, timeout=UPLOAD_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.error(
                            "Upload for job #%s timed out after %ss",
                            seq,
                            UPLOAD_TIMEOUT,
                        )
                        await self._reply_error(
                            seq,
                            f"上传超时（{UPLOAD_TIMEOUT} 秒）",
                            retry_job=self.jobs.get(seq),
                        )
                    except asyncio.CancelledError:
                        logger.info("Job #%s upload stopped by user", seq)
                        self._cancel_marked.discard(seq)
                        await self._delete_status(self.jobs.get(seq))
                    except Exception as exc:
                        logger.exception("Upload failed for job #%s", seq)
                        await self._reply_error(
                            seq, f"上传失败: {exc}", retry_job=self.jobs.get(seq)
                        )
                    finally:
                        self._upload_tasks.pop(seq, None)
                        self._uploading = None
            self._finish_seq(seq)

    def _finish_seq(self, seq: int) -> None:
        self._next_seq += 1
        self.results.pop(seq, None)
        self.jobs.pop(seq, None)
        self.active.pop(seq, None)
        self.active_seqs.discard(seq)
        shutil.rmtree(self._workdir(seq), ignore_errors=True)

    def _remember_published(self, seq: int, ids: list) -> None:
        self.published[seq] = ids
        if len(self.published) > 50:
            for old_seq in sorted(self.published)[:-50]:
                self.published.pop(old_seq, None)

    async def _publish(self, seq: int, payload) -> None:
        job = self.jobs[seq]
        if isinstance(payload, list):
            await self._publish_album(seq, payload)
            return

        path = payload
        size = os.path.getsize(path)
        if size > MAX_FILE_SIZE:
            await self._safe_edit(
                job,
                f"❌ 文件 {size / 1024 / 1024:.1f}MB 超过 "
                f"{MAX_FILE_SIZE // (1024 * 1024)}MB 上限",
            )
            return

        waiting = sum(1 for s, f in self.results.items() if s != seq and f.done())
        suffix = f"（另有 {waiting} 个待上传）" if waiting else ""
        label = "🔞 雪花遮挡" if job.spoiler else ""
        await self._safe_edit(
            job,
            f"✅ 下载完成（{size / 1024 / 1024:.1f}MB）{label}，正在上传{suffix}...",
        )

        caption = None
        if FORWARD_CAPTION and job.kind == "media":
            caption = job.message.message[:1024] or None
        msg_id = await self._send_media(path, caption, job.spoiler, job.seq)
        self._remember_published(seq, [msg_id])
        await self._safe_edit(
            job,
            f"✅ 已发布到 {DEST_CHANNEL}",
            buttons=[Button.inline("↩️ 撤销", f"undo:{seq}")],
        )
        if AUTO_DELETE_SECONDS > 0:
            asyncio.get_running_loop().create_task(
                _delete_after(job.status, AUTO_DELETE_SECONDS)
            )

    async def _publish_album(self, seq: int, paths: list) -> None:
        job = self.jobs[seq]
        for path in paths:
            size = os.path.getsize(path)
            if size > MAX_FILE_SIZE:
                await self._safe_edit(
                    job,
                    f"❌ 相册中有文件 {size / 1024 / 1024:.1f}MB 超过 "
                    f"{MAX_FILE_SIZE // (1024 * 1024)}MB 上限",
                )
                return

        total = sum(os.path.getsize(p) for p in paths)
        waiting = sum(1 for s, f in self.results.items() if s != seq and f.done())
        suffix = f"（另有 {waiting} 个待上传）" if waiting else ""
        label = "🔞 雪花遮挡" if job.spoiler else ""
        await self._safe_edit(
            job,
            f"✅ 相册下载完成（{len(paths)} 张，共 {total / 1024 / 1024:.1f}MB）"
            f"{label}，正在上传{suffix}...",
        )

        if FORWARD_CAPTION:
            captions = [m.message[:1024] or "" for m in job.album]
        else:
            captions = [""] * len(paths)
        ids = await self._send_album(paths, captions, job.spoiler, seq)
        self._remember_published(seq, ids)
        await self._safe_edit(
            job,
            f"✅ 相册已发布到 {DEST_CHANNEL}",
            buttons=[Button.inline("↩️ 撤销", f"undo:{seq}")],
        )
        if AUTO_DELETE_SECONDS > 0:
            asyncio.get_running_loop().create_task(
                _delete_after(job.status, AUTO_DELETE_SECONDS)
            )

    async def _send_media(self, path: str, caption: str | None, spoiler: bool, seq: int) -> int:
        media = await self._upload_media_input(path, spoiler, seq)
        msg = await self.client.send_file(DEST_CHANNEL, media, caption=caption)
        return msg.id

    async def _upload_media_input(
        self, path: str, spoiler: bool, seq: int, item: int = 1, items: int = 1
    ):
        progress = self._make_upload_progress(seq, item, items)
        uploaded = await self.client.upload_file(path, progress_callback=progress)

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
    ) -> list:
        dest = await self._get_dest_input()
        single_media = []
        total = len(paths)
        for index, path in enumerate(paths):
            fm = await self._upload_media_input(
                path, spoiler, seq, item=index + 1, items=total
            )

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

        result = await self.client(
            functions.messages.SendMultiMediaRequest(
                dest, multi_media=single_media
            )
        )
        ids = []
        for update in getattr(result, "updates", []) or []:
            if isinstance(update, types.UpdateNewChannelMessage):
                ids.append(update.message.id)
            elif isinstance(update, types.UpdateNewMessage):
                ids.append(update.message.id)
        return ids

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

    async def _cancel_pending(self, seq: int) -> bool:
        pending = self.pending.pop(seq, None)
        if pending is None:
            return False
        if pending.timeout_task is not None:
            pending.timeout_task.cancel()
        self._set_cancelled(seq)
        logger.info("Job #%s cancelled by user", seq)
        if pending.status is not None:
            try:
                await pending.status.delete()
            except Exception:
                pass
        return True

    async def _cancel_seq(self, seq: int) -> bool:
        if seq in self.pending:
            return await self._cancel_pending(seq)
        if (
            seq not in self.active_seqs
            and seq not in self._download_tasks
            and seq not in self._upload_tasks
            and seq not in self.jobs
        ):
            return False
        self._cancel_marked.add(seq)
        self._set_cancelled(seq)
        task = self._download_tasks.get(seq)
        if task is not None and not task.done():
            task.cancel()
        task = self._upload_tasks.get(seq)
        if task is not None and not task.done():
            task.cancel()
        job = self.jobs.get(seq)
        if job is not None:
            await self._delete_status(job)
        logger.info("Job #%s cancellation requested", seq)
        return True

    async def _auto_enqueue(
        self,
        kind: str,
        message: object,
        album: list,
        user_id: int,
        force_normal: bool = False,
    ) -> int:
        mode = self._spoiler_mode(user_id)
        if force_normal:
            spoiler = False
            label = "✅ 正常"
        else:
            spoiler = mode == "always_spoiler"
            label = "🔞 雪花遮挡" if spoiler else "✅ 正常"
        seq = self.reserve_seq()
        self.active_seqs.add(seq)
        try:
            status = await self.client.send_message(
                user_id,
                f"🔄 {self.task_label(seq)} 已按偏好自动处理：{label}",
            )
        except Exception:
            self._set_cancelled(seq)
            logger.error("Auto-enqueue failed for job #%s, seq settled", seq)
            raise
        self.enqueue(
            _Job(
                seq=seq,
                kind=kind,
                status=status,
                message=message,
                album=album,
                spoiler=spoiler,
                user_id=user_id,
            )
        )
        logger.info("Job #%s auto-enqueued spoiler=%s (mode=%s)", seq, spoiler, mode)
        return seq

    async def _show_ask(
        self,
        seq: int,
        kind: str,
        message: object,
        album: list,
        user_id: int,
        chat_id: int,
    ) -> None:
        self.register_pending(seq, kind, message, album=album, user_id=user_id)
        text = (
            f"⚠️ 该相册（{len(album)} 张）是否为 18+？"
            if kind == "album"
            else "⚠️ 该内容是否为 18+？"
        )
        try:
            status = await self.client.send_message(
                chat_id,
                text,
                buttons=[
                    [
                        Button.inline("🔞 是（雪花遮挡）", f"confirm:{seq}:1"),
                        Button.inline("✅ 否", f"confirm:{seq}:0"),
                    ],
                    [Button.inline("❌ 取消", f"cancel:{seq}")],
                ],
            )
            self.set_pending_status(seq, status)
        except Exception:
            self.pending.pop(seq, None)
            self._set_cancelled(seq)
            raise

    def hold_item(
        self, user_id: int, kind: str, message: object = None,
        album: list = None, chat_id: int = 0,
    ) -> bool:
        items = self.held.setdefault(user_id, [])
        items.append(_HeldItem(kind=kind, message=message, album=album, chat_id=chat_id))
        if len(items) == 1:
            self._held_timers[user_id] = asyncio.get_running_loop().create_task(
                self._held_timeout(user_id)
            )
            return True
        return False

    def take_held(self, user_id: int) -> list:
        timer = self._held_timers.pop(user_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
        return self.held.pop(user_id, [])

    async def _held_timeout(self, user_id: int) -> None:
        await asyncio.sleep(HELD_TIMEOUT)
        items = self.take_held(user_id)
        if not items:
            return
        logger.info("Held items for %s released by timeout as normal", user_id)
        for item in items:
            try:
                await self._auto_enqueue(
                    item.kind, item.message, item.album, user_id, force_normal=True
                )
            except Exception as exc:
                logger.exception("Held item timeout auto-enqueue failed: %s", exc)
        try:
            msg = await self.client.send_message(
                user_id,
                f"⏰ 未设置 18+ 模式，{len(items)} 个暂存内容已按「正常（非18+）」自动处理",
            )
            if AUTO_DELETE_SECONDS > 0:
                asyncio.get_running_loop().create_task(
                    _delete_after(msg, AUTO_DELETE_SECONDS)
                )
        except Exception:
            pass

    async def _release_held(self, user_id: int, mode: str, items: list) -> int:
        count = 0
        for item in items:
            try:
                if mode != "ask":
                    await self._auto_enqueue(
                        item.kind, item.message, item.album, user_id
                    )
                else:
                    seq = self.reserve_seq()
                    await self._show_ask(
                        seq, item.kind, item.message, item.album, user_id, item.chat_id
                    )
                count += 1
            except Exception as exc:
                logger.exception("Release held item failed: %s", exc)
        return count

    async def _reply_error(
        self, seq: int, text: str, retry_job: _Job = None
    ) -> None:
        job = self.jobs.get(seq)
        if retry_job is not None:
            self.retryable[seq] = retry_job
        buttons = None
        if retry_job is not None:
            buttons = [Button.inline("🔄 重试", f"retry:{seq}")]
        if job is not None:
            try:
                await job.status.edit(f"❌ {text}", buttons=buttons)
            except Exception:
                pass


def register_handlers(client: TelegramClient) -> None:
    pipeline = _Pipeline(client)
    pipeline.start()

    def _authorized(event: events.NewMessage.Event) -> bool:
        return event.sender_id in ALLOWED_USERS

    async def _respond(
        event: events.NewMessage.Event,
        text: str,
        auto_delete: bool = True,
        **kwargs,
    ) -> None:
        try:
            msg = await event.respond(text, **kwargs)
            if auto_delete and AUTO_DELETE_SECONDS > 0:
                asyncio.get_running_loop().create_task(
                    _delete_after(msg, AUTO_DELETE_SECONDS)
                )
        except Exception as exc:
            logger.warning("Respond failed: %s", exc)

    @client.on(events.NewMessage(pattern="/start"))
    async def on_start(event: events.NewMessage.Event) -> None:
        logger.info("CMD /start from %s", event.sender_id)
        if not _authorized(event):
            return
        if not pipeline._has_pref(event.sender_id, "spoiler_mode"):
            await _respond(event, 
                "📌 首次使用，请选择 18+ 处理方式（之后可用 /mode 修改）：",
                buttons=_mode_buttons(),
                auto_delete=False,
            )
            return
        mode = pipeline._spoiler_mode(event.sender_id)
        await _respond(event, 
            _START_TEXT + f"\n\n当前 18+ 模式：{_MODE_NAMES[mode]}（/mode 可修改）"
        )

    @client.on(events.NewMessage(pattern="/about"))
    async def on_about(event: events.NewMessage.Event) -> None:
        logger.info("CMD /about from %s", event.sender_id)
        if not _authorized(event):
            return
        await _respond(event, _ABOUT_TEXT)

    @client.on(events.NewMessage(pattern="/mode"))
    async def on_mode(event: events.NewMessage.Event) -> None:
        logger.info("CMD /mode from %s", event.sender_id)
        if not _authorized(event):
            return
        mode = pipeline._spoiler_mode(event.sender_id)
        await _respond(event, 
            f"当前 18+ 模式：{_MODE_NAMES[mode]}\n请选择新的处理方式：",
            buttons=_mode_buttons(),
            auto_delete=False,
        )

    @client.on(events.NewMessage(pattern="/status"))
    async def on_status(event: events.NewMessage.Event) -> None:
        logger.info("CMD /status from %s", event.sender_id)
        if not _authorized(event):
            return
        await _respond(event, pipeline.status_text(event.sender_id))

    @client.on(events.NewMessage(pattern="/progress"))
    async def on_progress(event: events.NewMessage.Event) -> None:
        logger.info("CMD /progress from %s", event.sender_id)
        if not _authorized(event):
            return
        if not pipeline._show_progress(event.sender_id):
            await _respond(event, 
                "🔕 进度条显示已关闭。点击任意任务状态消息的"
                "「🔔 显示进度」按钮可重新开启。"
            )
            return
        lines = []
        for seq, info in sorted(pipeline.active.items()):
            phase, item, items, pct = (
                info["phase"],
                info["item"],
                info["items"],
                info["pct"],
            )
            if items > 1:
                label = (
                    f"{pipeline.task_label(seq)} ⬇ 下载 {item}/{items}"
                    if phase == "download"
                    else f"{pipeline.task_label(seq)} 📤 上传 {item}/{items}"
                )
            else:
                label = (
                    f"{pipeline.task_label(seq)} ⬇ 下载"
                    if phase == "download"
                    else f"{pipeline.task_label(seq)} 📤 上传"
                )
            lines.append(f"{label} {render_bar(pct)} {pct:3d}%")
        if not lines:
            await _respond(event, "📊 暂无进行中的任务")
        else:
            await _respond(event, "📊 进行中任务\n" + "\n".join(lines))

    @client.on(events.NewMessage(pattern="/pause"))
    async def on_pause(event: events.NewMessage.Event) -> None:
        logger.info("CMD /pause from %s", event.sender_id)
        if not _authorized(event):
            return
        pipeline._paused = True
        await _respond(event, "⏸ 已暂停队列（当前步骤完成后暂停，新的任务不再开始）")

    @client.on(events.NewMessage(pattern="/resume"))
    async def on_resume(event: events.NewMessage.Event) -> None:
        logger.info("CMD /resume from %s", event.sender_id)
        if not _authorized(event):
            return
        pipeline._paused = False
        await _respond(event, "▶ 已恢复队列")

    @client.on(events.NewMessage(pattern="/queue"))
    async def on_queue(event: events.NewMessage.Event) -> None:
        logger.info("CMD /queue from %s", event.sender_id)
        if not _authorized(event):
            return
        lines = ["📋 队列管理"]
        buttons = []

        active_lines = []
        for seq in sorted(pipeline.active_seqs):
            pos = pipeline.task_label(seq)
            info = pipeline.active.get(seq)
            show = pipeline._show_progress(event.sender_id)
            if seq in pipeline._download_tasks:
                if info and show:
                    prefix = (
                        f"⬇ 下载 {info['item']}/{info['items']}"
                        if info["items"] > 1
                        else "🔄 正在下载"
                    )
                    state = f"{pos} {prefix} {render_bar(info['pct'])} {info['pct']:3d}%"
                else:
                    state = f"{pos} 🔄 正在下载"
            elif seq == pipeline._uploading:
                if info and show:
                    prefix = (
                        f"📤 上传 {info['item']}/{info['items']}"
                        if info["items"] > 1
                        else "📤 正在上传"
                    )
                    state = f"{pos} {prefix} {render_bar(info['pct'])} {info['pct']:3d}%"
                else:
                    state = f"{pos} 📤 正在上传"
            elif seq in pipeline.jobs:
                state = f"{pos} ✅ 等待上传"
            else:
                state = f"{pos} ⏳ 等待下载"
            active_lines.append(state)
            buttons.append([Button.inline("⏹ 取消", f"q_cancel:{seq}")])

        if active_lines:
            lines.append(f"\n▶ 进行中（{len(active_lines)}）")
            lines.extend(active_lines)
        else:
            lines.append("\n▶ 进行中：无")

        pending_lines = []
        for seq in sorted(pipeline.pending):
            p = pipeline.pending[seq]
            kind_label = "相册" if p.kind == "album" else "媒体"
            pending_lines.append(f"⏳ 待确认（{kind_label}）")
            buttons.append([Button.inline("❌ 取消", f"cancel:{seq}")])
        if pending_lines:
            lines.append("\n❓ 待确认")
            lines.extend(pending_lines)

        buttons.append(
            [
                Button.inline("⏸ 暂停", "q_pause"),
                Button.inline("▶ 恢复", "q_resume"),
            ]
        )
        await _respond(
            event, "\n".join(lines), buttons=buttons, auto_delete=False
        )

    @client.on(events.NewMessage(pattern=r"/cancel\s+(\d+)"))
    async def on_cancel(event: events.NewMessage.Event) -> None:
        logger.info("CMD /cancel from %s", event.sender_id)
        if not _authorized(event):
            return
        n = int(event.pattern_match.group(1))
        pendings = sorted(pipeline.pending)
        if n < 1 or n > len(pendings):
            await _respond(event, 
                f"❌ 没有第 {n} 个待确认项（当前 {len(pendings)} 个）"
            )
            return
        seq = pendings[n - 1]
        await pipeline._cancel_pending(seq)
        await _respond(event, f"❌ 已取消第 {n} 个待确认项")

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

        data_text = event.data.decode(errors="replace")
        if data_text.startswith("mode:"):
            mode = data_text.split(":", 1)[1]
            if mode not in _MODE_NAMES:
                await _answer("无效操作")
                return
            pipeline.set_spoiler_mode(event.sender_id, mode)
            held = pipeline.take_held(event.sender_id)
            released = 0
            if held:
                released = await pipeline._release_held(
                    event.sender_id, mode, held
                )
            await _answer(f"已设置：{_MODE_NAMES[mode]}")
            try:
                text = f"✅ 已设置 18+ 模式：{_MODE_NAMES[mode]}"
                if released:
                    text += f"\n已处理 {released} 个暂存内容"
                await event.edit(text)
            except Exception:
                pass
            return

        if data_text.startswith("cancel:"):
            try:
                seq = int(data_text.split(":", 1)[1])
            except (ValueError, IndexError):
                await _answer("无效操作")
                return
            ok = await pipeline._cancel_pending(seq)
            await _answer("已取消" if ok else "该确认已失效")
            return

        if data_text.startswith("q_cancel:"):
            try:
                seq = int(data_text.split(":", 1)[1])
            except (ValueError, IndexError):
                await _answer("无效操作")
                return
            ok = await pipeline._cancel_seq(seq)
            await _answer("已取消" if ok else "无法取消")
            return

        if event.data == b"q_pause":
            pipeline._paused = True
            await _answer("已暂停")
            return

        if event.data == b"q_resume":
            pipeline._paused = False
            await _answer("已恢复")
            return

        if data_text.startswith("stop:"):
            try:
                seq = int(data_text.split(":", 1)[1])
            except (ValueError, IndexError):
                await _answer("无效操作")
                return
            task = pipeline._download_tasks.get(seq)
            if task is None or task.done():
                task = pipeline._upload_tasks.get(seq)
            if task is not None and not task.done():
                task.cancel()
                await _answer("正在停止...")
            else:
                await _answer("该任务不在下载/上传中")
            return

        if data_text.startswith("undo:"):
            try:
                seq = int(data_text.split(":", 1)[1])
            except (ValueError, IndexError):
                await _answer("无效操作")
                return
            ids = pipeline.published.pop(seq, None)
            if not ids:
                await _answer("该发布已无法撤销")
                return
            try:
                await event.client.delete_messages(DEST_CHANNEL, ids)
            except Exception as exc:
                await _answer(f"撤销失败: {exc}")
                return
            logger.info("Undo published job #%s ids=%s", seq, ids)
            try:
                await event.delete()
            except Exception:
                pass
            await _answer("已撤销")
            return

        if data_text.startswith("retry:"):
            try:
                seq = int(data_text.split(":", 1)[1])
            except (ValueError, IndexError):
                await _answer("无效操作")
                return
            old_job = pipeline.retryable.pop(seq, None)
            if old_job is None:
                await _answer("该任务已失效（可能已重试）")
                return
            new_seq = pipeline.reserve_seq()
            new_job = _Job(
                seq=new_seq,
                kind=old_job.kind,
                status=old_job.status,
                message=old_job.message,
                album=old_job.album,
                url=old_job.url,
                spoiler=old_job.spoiler,
                user_id=old_job.user_id,
            )
            pipeline.active_seqs.add(new_seq)
            try:
                new_status = await event.client.send_message(
                    event.chat_id,
                    f"🔄 {pipeline.task_label(new_seq)} 已重新入队",
                )
            except Exception:
                new_status = old_job.status
            new_job.status = new_status
            pipeline.enqueue(new_job)
            try:
                await event.edit("🔄 已重新入队")
            except Exception:
                pass
            await _answer("已重新入队")
            logger.info("Job #%s retried as #%s", seq, new_seq)
            return

        if event.data == b"toggle_progress":
            show = pipeline.toggle_progress_pref(event.sender_id)
            await _answer("进度条已开启" if show else "进度条已关闭")
            try:
                await event.edit(
                    "🔔 进度条显示已开启" if show else "🔕 进度条显示已关闭"
                )
            except Exception:
                pass
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
        pipeline.active_seqs.add(seq)
        try:
            new_status = await event.client.send_message(
                event.chat_id,
                f"🔄 {pipeline.task_label(seq)} 已确认：{label}",
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
                    user_id=pending.user_id,
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

        if any(
            isinstance(e, MessageEntityBotCommand)
            for e in (event.message.entities or [])
        ):
            return

        if isinstance(event.message.media, MEDIA_TYPES):
            grouped_id = event.message.grouped_id
            if grouped_id:
                logger.info("Album message -> collect_album gid=%s", grouped_id)
                pipeline.collect_album(grouped_id, event.message, event.chat_id)
                return
            if not pipeline._has_pref(event.sender_id, "spoiler_mode"):
                first = pipeline.hold_item(
                    event.sender_id, "media",
                    message=event.message, chat_id=event.chat_id,
                )
                if first:
                    await _respond(
                        event,
                        "📌 请先设置 18+ 处理方式，再继续处理视频：",
                        buttons=_mode_buttons(),
                        auto_delete=False,
                    )
                else:
                    await _respond(event, "⏳ 已暂存，等待你设置 18+ 模式")
                return
            if pipeline._spoiler_mode(event.sender_id) != "ask":
                try:
                    await pipeline._auto_enqueue(
                        "media", event.message, None, event.sender_id
                    )
                except Exception as exc:
                    logger.exception("Auto-enqueue failed: %s", exc)
                    await _respond(event, f"自动处理失败: {exc}")
                return
            seq = pipeline.reserve_seq()
            try:
                await pipeline._show_ask(
                    seq, "media", event.message, None,
                    event.sender_id, event.chat_id,
                )
            except Exception as exc:
                logger.exception("18+ question FAILED for #%s: %s", seq, exc)
                await _respond(event, f"发送确认失败: {exc}")
            return

        url_match = URL_RE.search(event.raw_text or "")
        if url_match:
            status = await event.reply("⏳ 正在加入队列...")
            seq = pipeline.submit(
                "url", status, event.message, url_match.group(0),
                user_id=event.sender_id,
            )
            await status.edit(f"⏳ {pipeline.task_label(seq)} 已加入队列")
            return

        await _respond(event, "请发送视频或链接，或使用 /start 查看使用说明。")
