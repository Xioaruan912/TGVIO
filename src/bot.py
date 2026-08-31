import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

from telethon import Button, TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
)

from .config import (
    SETTINGS,
    Settings,
    ALLOWED_USERS,
    AUTO_DELETE_SECONDS,
    CHANNEL_AT,
    COLLECTION_GATHER_SECONDS,
    CACHE_RETENTION_HOURS,
    CONFIRM_TIMEOUT,
    COVER_MODE,
    COVER_WIDTH,
    DISK_ENFORCE,
    DEST_CHANNEL,
    DOWNLOAD_AUTO_RETRY,
    DOWNLOAD_CONCURRENCY,
    DOWNLOAD_DIR,
    DOWNLOAD_TIMEOUT,
    DOWNLOAD_WORKERS,
    FORWARD_CAPTION,
    FAILED_CACHE_RETENTION_HOURS,
    GROUP_AT,
    MAX_COVER_IMAGES,
    MAX_FILE_SIZE,
    MAX_CACHE_BYTES,
    MEDIA_COMPAT_MODE,
    MIN_FREE_BYTES,
    MIN_FREE_PERCENT,
    PART_SIZE_KB,
    PROGRESS_MIN_INTERVAL,
    SESSION_COLLECT,
    SESSION_END_TIMEOUT,
    UPLOAD_TIMEOUT,
    UPLOAD_WORKERS,
    UNKNOWN_JOB_RESERVE_BYTES,
    FASTSTART_MAX_BYTES,
    TRANSCODE_ENABLED,
    THUMBNAIL_POSITION,
    WEBDAV_ENABLED,
    WEBDAV_PASS,
    WEBDAV_PATH,
    WEBDAV_RETRY,
    WEBDAV_URL,
    WEBDAV_USER,
)
from .domain import ErrorCode, RetryPolicy, classify_error, safe_traceback
from . import webdav
from .media import FileTooLargeError, MediaDownloader, MediaPublisher, PublishPartialError
from .models import AlbumBuffer, Job, PendingJob, RetryInfo, Session
from .progress import ProgressTracker, position_token, render_bar
from .storage import JsonStore
from .handlers import HandlerContext, install_handlers
from .services import (
    BackupManager,
    InteractionSessions,
    DiskManager,
    JobQueue,
    NetworkCoordinator,
    OperationStore,
    ProxyManager,
    ShadowState,
    DedupManager,
    DestinationProfileManager,
    SourceProfileManager,
    MediaCompatibilityManager,
    StatsService,
    recover_jobs,
)
from .views import (
    JobCardView,
    PendingQueueItemView,
    ProxyViewState,
    QueueItemView,
    QueueViewState,
    WebDavConfigViewState,
    proxy_list_view as render_proxy_list_view,
    proxy_view as render_proxy_view,
    job_card_view as render_job_card_view,
    queue_view as render_queue_view,
    webdav_cfg_fields_view as render_webdav_cfg_fields_view,
    webdav_cfg_lines as render_webdav_cfg_lines,
    webdav_cfg_view as render_webdav_cfg_view,
)

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MEDIA_TYPES = (MessageMediaPhoto, MessageMediaDocument)

_CANCELLED = object()

PREFS_FILE = os.path.join("session", "prefs.json")
LEGACY_PREFS_FILE = os.path.join("session", "progress_prefs.json")
WEBDAV_CFG_FILE = os.path.join("session", "webdav.json")
WEBDAV_LOGS_FILE = os.path.join("session", "webdav_logs.json")
WEBDAV_COUNT_FILE = os.path.join("session", "webdav_count.json")
WEBDAV_LOG_HOURS = 24
WEBDAV_AUTORETRY_INTERVAL = 3600  # 失败记录自动重传间隔（秒，默认 1 小时）
PROXY_FILE = os.path.join("session", "proxy.json")

def _file_md5_short(path: str) -> str:
    """文件内容 MD5 前 8 位（分块读取，大文件不占内存）。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            c = f.read(1024 * 1024)
            if not c:
                break
            h.update(c)
    return h.hexdigest()[:8]

def queue_view(pipeline, user_id: int):
    """Compatibility entry point backed by the immutable R1 queue view model."""
    return render_queue_view(pipeline._queue_view_state(user_id))


def _pos_token(n: int) -> str:
    return position_token(n)


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
    "1. 转发含视频/图片的消息给我 → 自动开始合集会话，继续转发自动并入 → 完成后发 /end 或点「🛑 结束并发布」按钮发布到频道\n"
    "2. 发送一个链接（抖音/B站/YouTube 等）→ 自动下载并发布到频道\n\n"
    "合集：图片进频道封面相册（超过 10 张按序丢弃），全部视频整合进同一个评论区；\n"
    "会话期间发的文字消息会作为评论，结束时整合为封面文字与封面一起发送。\n"
    "合集进行中只显示一条状态消息，不会随每次转发反复弹出；发 /end 结束。\n\n"
    "18+ 处理默认「总是正常」；需要雪花遮挡请用 /mode 设置「总是雪花遮挡」或「每次询问」。\n"
    "选「是（雪花遮挡）」时，用 Telegram 内置雪花效果遮挡发布，文件内容不被修改。\n\n"
    f"⚠️ 确认弹窗 {CONFIRM_TIMEOUT} 秒内未回复将自动按正常（非 18+）模式处理。\n"
    f"⚠️ 单个上传文件上限 {MAX_FILE_SIZE // (1024 * 1024)}MB（平台上限）\n\n"
    "ℹ️ 关于：视频转发机器人，把转发内容处理后发布到频道。输入 /about 查看全部命令说明。"
)

_ABOUT_TEXT = (
    "ℹ️ 关于 · 视频转发机器人\n\n"
    f"把转发的视频/图片/链接处理后发布到 {DEST_CHANNEL}。\n"
    "支持 18+ 雪花遮挡、合集会话、相册聚合、并行下载/顺序上传队列、\n"
    "进度条、撤销发布与失败重试。\n\n"
    "📖 命令说明：\n"
    "/start    使用说明\n"
    "/about    关于/命令说明\n"
    "/mode     设置 18+ 处理方式（默认总是正常，可改每次询问/总是雪花/总是正常）\n"
    "/webdav   配置 WebDAV 备份链接\n"
    "/webdavlogs  查看上传记录 / 本地待上传缓存\n"
    "/proxy    代理设置（HTTP 代理，下载失败自动切换）\n"
    "/queue    管理队列（逐项取消/暂停/恢复）\n"
    "/begin    开始合集会话（转发会自动开始）\n"
    "/end      结束合集并发布（所有视频进同一个评论区）"
)


_Job = Job
_RetryInfo = RetryInfo
_PendingJob = PendingJob
_AlbumBuffer = AlbumBuffer
_Session = Session


def _legacy_static_settings() -> Settings:
    """Build one compatibility snapshot from legacy re-export constants.

    Production startup injects strict ``Settings`` from ``main``.  This path
    intentionally preserves one migration cycle for tests/embedders that patch
    the old module constants before calling ``register_handlers``.
    """
    return replace(
        SETTINGS,
        allowed_users=frozenset(ALLOWED_USERS),
        auto_delete_seconds=AUTO_DELETE_SECONDS,
        channel_at=CHANNEL_AT,
        group_at=GROUP_AT,
        max_file_size=MAX_FILE_SIZE,
        download_dir=DOWNLOAD_DIR,
        download_concurrency=DOWNLOAD_CONCURRENCY,
        download_timeout=DOWNLOAD_TIMEOUT,
        confirm_timeout=CONFIRM_TIMEOUT,
        collection_gather_seconds=COLLECTION_GATHER_SECONDS,
        upload_timeout=UPLOAD_TIMEOUT,
        forward_caption=FORWARD_CAPTION,
        progress_min_interval=PROGRESS_MIN_INTERVAL,
        download_auto_retry=DOWNLOAD_AUTO_RETRY,
        download_workers=DOWNLOAD_WORKERS,
        upload_workers=UPLOAD_WORKERS,
        part_size_kb=PART_SIZE_KB,
        cover_mode=COVER_MODE,
        cover_width=COVER_WIDTH,
        max_cover_images=MAX_COVER_IMAGES,
        session_collect=SESSION_COLLECT,
        session_end_timeout=SESSION_END_TIMEOUT,
        disk_enforce=DISK_ENFORCE,
        min_free_bytes=MIN_FREE_BYTES,
        min_free_percent=MIN_FREE_PERCENT,
        max_cache_bytes=MAX_CACHE_BYTES,
        cache_retention_hours=CACHE_RETENTION_HOURS,
        failed_cache_retention_hours=FAILED_CACHE_RETENTION_HOURS,
        unknown_job_reserve_bytes=UNKNOWN_JOB_RESERVE_BYTES,
        media_compat_mode=MEDIA_COMPAT_MODE,
        faststart_max_bytes=FASTSTART_MAX_BYTES,
        transcode_enabled=TRANSCODE_ENABLED,
        thumbnail_position=THUMBNAIL_POSITION,
        dest_channel=DEST_CHANNEL,
    )


class _Pipeline:
    def __init__(self, client: TelegramClient, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings or _legacy_static_settings()
        static = self.settings
        self.download_dir = static.download_dir
        self.input_q: asyncio.Queue = asyncio.Queue()
        self._runtime_jobs: dict[int, _Job] = {}
        self.jobs: dict[int, _Job] = {}
        self.pending: dict[int, _PendingJob] = {}
        self.albums: dict[int, _AlbumBuffer] = {}
        self.album_jobs: dict[int, _Job] = {}
        self.pending_albums: dict[int, int] = {}
        self.results: dict[int, asyncio.Future] = {}
        self.active: dict[int, dict] = {}
        self.active_seqs: set[int] = set()
        self._counter = int(time.time())
        self._dest_input = None
        self._active_downloads = 0
        self._uploading: int | None = None
        self._download_tasks: dict[int, asyncio.Task] = {}
        self._upload_tasks: dict[int, asyncio.Task] = {}
        self._repo_download_claimed: set[int] = set()
        self._repo_publish_claimed: set[int] = set()
        self._repo_claim_owners: dict[tuple[str, int], str] = {}
        self._worker_tasks: set[asyncio.Task] = set()
        self._stopping = False
        self._shutdown_complete = False
        self.prefs: dict[int, dict] = {}
        self.published: dict[int, list] = {}
        self.retryable: dict[int, _Job] = {}
        self._paused = False
        self._progress_tracker = ProgressTracker(ui_interval=static.progress_min_interval)
        self._retry_policy = RetryPolicy(budgets={"download": static.download_auto_retry})
        self._retry_sleep = asyncio.sleep
        self._retry_interrupts: dict[int, asyncio.Event] = {}
        self._status_rebound: set[int] = set()
        self._cancel_marked: set[int] = set()
        self._paused_files: set[int] = set()
        self._future_created: dict[int, float] = {}
        self.sessions: dict[int, _Session] = {}
        self._webdav_tasks: dict[asyncio.Task, int] = {}
        self.webdav_cfg = self._load_webdav_cfg()
        self.webdav_logs: dict = self._load_webdav_logs()
        self.webdav_keep_cache: set[int] = set()
        self.webdav_count: dict = self._load_webdav_count()
        self._webdav_count_lock = asyncio.Lock()
        self.proxy_cfg: dict = self._load_proxy_cfg()
        self.disk = DiskManager(
            self.download_dir,
            enforce=static.disk_enforce,
            min_free_bytes=static.min_free_bytes,
            min_free_percent=static.min_free_percent,
            max_cache_bytes=static.max_cache_bytes,
            unknown_reserve_bytes=static.unknown_job_reserve_bytes,
        )
        self._disk_cache_retention_hours = static.cache_retention_hours
        self._disk_failed_retention_hours = static.failed_cache_retention_hours
        self.network = NetworkCoordinator(
            proxies=lambda: self.proxy_cfg.get("proxies", []),
            current=lambda: int(self.proxy_cfg.get("current", -1)),
            auto_enabled=lambda: bool(self.proxy_cfg.get("auto")),
            apply_proxy=self._apply_proxy_uncoordinated,
        )
        self.media_compat = MediaCompatibilityManager(
            mode=static.media_compat_mode,
            faststart_max_bytes=static.faststart_max_bytes,
            transcode_enabled=static.transcode_enabled,
            disk=self.disk,
        )
        self._load_prefs()

        self.downloader = MediaDownloader(
            client,
            self._workdir,
            static.download_timeout,
            static.download_workers,
            static.part_size_kb,
            static.url_private_network_policy,
        )
        self.publisher = MediaPublisher(
            client,
            static.dest_channel,
            self._workdir,
            static.upload_timeout,
            static.max_file_size,
            static.forward_caption,
            static.upload_workers,
            static.part_size_kb,
            cover_mode=static.cover_mode,
            cover_width=static.cover_width,
            max_cover_images=static.max_cover_images,
            group_counter_file=os.path.join("session", "group_counter.txt"),
            channel_at=static.channel_at,
            group_at=static.group_at,
            thumbnail_position=static.thumbnail_position,
        )
        self.downloader.pre_download_hooks.append(self._on_pre_download)
        self.downloader.progress_hooks.append(self._on_download_progress)
        self.downloader.status_hooks.append(self._on_download_status)
        self.downloader.post_download_hooks.append(self._on_media_compat)
        self.downloader.post_download_hooks.append(self._on_download_done)
        self.downloader.post_download_hooks.append(self._on_dedup_hash)
        self.downloader.post_download_hooks.append(self._on_webdav_upload)
        self.publisher.progress_hooks.append(self._on_upload_progress)
        self.publisher.pre_publish_hooks.append(self._on_pre_publish)
        self.publisher.checkpoint_hooks.append(self._on_publish_checkpoint)
        self.publisher.post_publish_hooks.append(self._on_published)

    def _load_prefs(self) -> None:
        data = JsonStore(PREFS_FILE, {}).load()
        self.prefs = (
            {int(k): dict(v) for k, v in data.items()}
            if isinstance(data, dict)
            else {}
        )
        if not self.prefs:
            try:
                legacy = JsonStore(LEGACY_PREFS_FILE, {}).load()
                for uid, show in legacy.items():
                    self.prefs.setdefault(int(uid), {})["show_progress"] = bool(show)
                if self.prefs:
                    self._save_prefs()
            except Exception:
                pass

    def _save_prefs(self) -> None:
        JsonStore(PREFS_FILE, {}).save({str(k): v for k, v in self.prefs.items()})

    def _get_pref(self, user_id: int, key: str, default):
        return self.prefs.get(user_id, {}).get(key, default)

    def _set_pref(self, user_id: int, key: str, value) -> None:
        self.prefs.setdefault(user_id, {})[key] = value
        self._save_prefs()

    def _load_webdav_cfg(self) -> dict:
        cfg = {
            "enabled": WEBDAV_ENABLED,
            "url": WEBDAV_URL,
            "user": WEBDAV_USER,
            "pass": WEBDAV_PASS,
            "path": WEBDAV_PATH,
            "retry": WEBDAV_RETRY,
            "backup_policy": "best_effort",
        }
        data = JsonStore(WEBDAV_CFG_FILE, {}).load()
        if isinstance(data, dict):
            for key in cfg:
                if key in data:
                    cfg[key] = data[key]
        return cfg

    def _save_webdav_cfg(self) -> None:
        JsonStore(WEBDAV_CFG_FILE, {}).save(self.webdav_cfg)

    def _load_webdav_logs(self) -> dict:
        data = JsonStore(WEBDAV_LOGS_FILE, {}).load()
        return data if isinstance(data, dict) else {}

    def _save_webdav_logs(self) -> None:
        cutoff = time.time() - WEBDAV_LOG_HOURS * 3600
        self.webdav_logs = {
            k: v
            for k, v in self.webdav_logs.items()
            if isinstance(v, dict) and v.get("ts", 0) >= cutoff
        }
        JsonStore(WEBDAV_LOGS_FILE, {}).save(self.webdav_logs)

    def _load_webdav_count(self) -> dict:
        data = JsonStore(WEBDAV_COUNT_FILE, {}).load()
        return data if isinstance(data, dict) else {}

    def _save_webdav_count(self) -> None:
        # 只保留最近 7 天的计数，避免文件无限增长
        self.webdav_count = {
            k: v
            for k, v in self.webdav_count.items()
            if isinstance(v, int) and k >= (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        }
        JsonStore(WEBDAV_COUNT_FILE, {}).save(self.webdav_count)

    def _load_proxy_cfg(self) -> dict:
        cfg = {"auto": True, "current": -1, "proxies": []}
        data = JsonStore(PROXY_FILE, {}).load()
        if isinstance(data, dict):
            cfg["auto"] = bool(data.get("auto", True))
            try:
                cfg["current"] = int(data.get("current", -1))
            except (TypeError, ValueError):
                cfg["current"] = -1
            proxies = data.get("proxies", [])
            cfg["proxies"] = (
                [p for p in proxies if isinstance(p, dict) and p.get("url")]
                if isinstance(proxies, list)
                else []
            )
        return cfg

    def _save_proxy_cfg(self) -> None:
        JsonStore(PROXY_FILE, {}).save(self.proxy_cfg)

    @staticmethod
    def _parse_proxy_url(url: str):
        """解析 http 代理 URL → Telethon proxy 元组 ("http", host, port, user, pwd, rdns)。无效返回 None。"""
        m = re.match(r"^http://([^@/]+@)?([^:/]+):(\d+)$", (url or "").strip())
        if not m:
            return None
        userinfo = (m.group(1) or "").rstrip("@")
        host = m.group(2)
        port = int(m.group(3))
        username = password = None
        if userinfo:
            if ":" in userinfo:
                username, password = userinfo.split(":", 1)
            else:
                username = userinfo
        return ("http", host, port, username, password, True)

    def _proxy_label(self, idx: int) -> str:
        proxies = self.proxy_cfg.get("proxies", [])
        if idx < 0 or idx >= len(proxies):
            return "直连"
        url = proxies[idx].get("url", "")
        parsed = self._parse_proxy_url(url)
        if parsed is None:
            return f"代理 #{idx + 1}（地址无效）"
        _, host, port, username, _password, _rdns = parsed
        auth = "***@" if username else ""
        return f"http://{auth}{host}:{port}"

    def _proxy_view_state(self) -> ProxyViewState:
        proxies = self.proxy_cfg.get("proxies", [])
        return ProxyViewState(
            current=self.proxy_cfg.get("current", -1),
            auto=bool(self.proxy_cfg.get("auto")),
            labels=tuple(self._proxy_label(idx) for idx in range(len(proxies))),
            current_label=self._proxy_label(self.proxy_cfg.get("current", -1)),
        )

    async def _apply_proxy_uncoordinated(self, idx: int) -> bool:
        """应用代理（idx=-1 直连）：改 client._proxy + 重建连接，session 保留免重登。"""
        previous_proxy = getattr(self.client, "_proxy", None)
        try:
            proxy = None
            if idx >= 0:
                proxies = self.proxy_cfg.get("proxies", [])
                if idx >= len(proxies):
                    return False
                proxy = self._parse_proxy_url(proxies[idx].get("url", ""))
                if proxy is None:
                    logger.warning("Proxy #%s URL 无效，无法应用", idx)
                    return False
            self.client._proxy = proxy
            try:
                await self.client.disconnect()
            except Exception:
                pass
            await self.client.connect()
            self.proxy_cfg["current"] = idx
            self._save_proxy_cfg()
            logger.info("Applied proxy #%s (%s)", idx, self._proxy_label(idx))
            return True
        except Exception as exc:
            self.client._proxy = previous_proxy
            try:
                checker = getattr(self.client, "is_connected", None)
                connected = bool(checker()) if callable(checker) else False
                if not connected:
                    await self.client.connect()
            except Exception:
                pass
            logger.warning("Proxy switch to #%s failed: %s", idx, exc)
            return False

    async def _apply_proxy(self, idx: int) -> bool:
        """Serialized public proxy apply path."""
        result = await self.network.apply(idx)
        return result.switched

    async def apply_proxy_on_start(self) -> None:
        """启动时恢复上次的代理（若配置了 current >= 0）。"""
        current = self.proxy_cfg.get("current", -1)
        proxies = self.proxy_cfg.get("proxies", [])
        if current >= 0 and current < len(proxies):
            if await self._apply_proxy(current):
                logger.info("Proxy applied on start: #%s", current)
            else:
                logger.warning("Proxy #%s failed on start, back to direct", current)
                await self._apply_proxy(-1)
        else:
            self.proxy_cfg["current"] = -1
            self._save_proxy_cfg()

    async def _try_switch_proxy(self, seq: int) -> bool:
        """Compatibility wrapper; new workers pass the observed generation."""
        result = await self.network.auto_switch(
            observed_generation=self.network.generation,
        )
        if result.switched:
            logger.info("Job #%s 网络失败，已自动切换代理 #%s", seq, result.index)
        elif result.reevaluate:
            logger.info("Job #%s 网络配置已由其它任务更新，直接在新连接重试", seq)
        return result.switched

    @staticmethod
    def _test_http_proxy(url: str) -> bool:
        """通过代理访问测试连通性（urllib 标准库，http/https 任一成功即可）。"""
        import urllib.request

        handler = urllib.request.ProxyHandler({"http": url, "https": url})
        opener = urllib.request.build_opener(handler)
        for target in ("http://api.ipify.org", "https://api.ipify.org"):
            try:
                with opener.open(target, timeout=6) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                continue
        return False

    def _proxy_view(self) -> tuple:
        """/proxy 主视图。"""
        return render_proxy_view(self._proxy_view_state())

    def _proxy_list_view(self) -> tuple:
        """/proxy 管理列表。"""
        return render_proxy_list_view(self._proxy_view_state())

    def _show_progress(self, user_id: int) -> bool:
        return self._get_pref(user_id, "show_progress", True)

    def toggle_progress_pref(self, user_id: int) -> bool:
        current = self._show_progress(user_id)
        self._set_pref(user_id, "show_progress", not current)
        return not current

    def _spoiler_mode(self, user_id: int) -> str:
        explicit = self.prefs.get(user_id, {}).get("spoiler_mode")
        if explicit in {"ask", "always_normal", "always_spoiler"}:
            return explicit
        profiles = getattr(self, "destination_profiles", None)
        if profiles is not None:
            value = str(profiles.current_profile.default_spoiler_mode or "always_normal")
            if value in {"ask", "always_normal", "always_spoiler"}:
                return value
        return "always_normal"

    def set_spoiler_mode(self, user_id: int, mode: str) -> None:
        self._set_pref(user_id, "spoiler_mode", mode)

    def start(self) -> None:
        if self._worker_tasks or self._stopping:
            return
        loop = asyncio.get_running_loop()
        for _ in range(max(1, DOWNLOAD_CONCURRENCY)):
            self._track_worker(loop.create_task(self._download_worker()))
        self._track_worker(loop.create_task(self._upload_worker()))
        self._track_worker(loop.create_task(self._webdav_autoretry_loop()))

    def _track_worker(self, task: asyncio.Task) -> None:
        self._worker_tasks.add(task)
        task.add_done_callback(self._worker_tasks.discard)

    async def shutdown(self, timeout: float = 20.0) -> None:
        """Stop claims first, durably interrupt active work, then stop tasks."""
        if self._shutdown_complete:
            return
        self._stopping = True

        # Persist claim settlement before any transport task is cancelled.
        if getattr(self, "job_queue", None) is not None:
            await self.job_queue.drain_shadow()
            for (kind, seq), owner in list(self._repo_claim_owners.items()):
                try:
                    await self.job_queue.interrupt_claim(seq, kind, owner)
                except Exception:
                    logger.exception("Failed to interrupt %s claim for job #%s", kind, seq)

        transport = {
            task
            for task in [*self._download_tasks.values(), *self._upload_tasks.values()]
            if task is not None and not task.done()
        }
        for task in transport:
            task.cancel()
        if transport:
            await asyncio.gather(*transport, return_exceptions=True)

        # Give in-flight WebDAV writes a bounded chance to finish, then cancel.
        webdav = {task for task in self._webdav_tasks if not task.done()}
        if webdav:
            done, pending = await asyncio.wait(webdav, timeout=min(10.0, max(0.0, timeout / 2)))
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        for pending in self.pending.values():
            if pending.timeout_task is not None:
                pending.timeout_task.cancel()
        for buf in self.albums.values():
            if buf.task is not None:
                buf.task.cancel()
        for session in self.sessions.values():
            if session.button_task is not None:
                session.button_task.cancel()

        workers = {task for task in self._worker_tasks if not task.done()}
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if getattr(self, "job_queue", None) is not None:
            await self.job_queue.drain_shadow()
        self._shutdown_complete = True

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
            elif seq in self._paused_files:
                active_lines.append(f"{pos} ⏸ 已暂停")
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
        if self.sessions:
            total = sum(s.media_count for s in self.sessions.values())
            texts = sum(s.text_count for s in self.sessions.values())
            extras.append(
                f"📦 合集进行中: {len(self.sessions)} 项（{total} 个媒体 · {texts} 条评论）"
            )
        if extras:
            lines.append("\n▶ 其他")
            lines.extend(extras)

        return "\n".join(lines)

    def submit(
        self, kind: str, status: object, message: object, url: str = "", user_id: int = 0
    ) -> int:
        seq = self.reserve_seq()
        profiles = getattr(self, "destination_profiles", None)
        profile = profiles.current_profile if profiles is not None else None
        profile_snapshot = profiles.current_snapshot() if profiles is not None else None
        self.enqueue(
            _Job(
                seq=seq,
                kind=kind,
                status=status,
                message=message,
                url=url,
                user_id=user_id,
                destination_profile_id=(profile.id if profile is not None else None),
                destination_profile_snapshot=profile_snapshot,
            )
        )
        if getattr(self, "job_queue", None) is not None:
            self.job_queue.shadow_accept_legacy(
                seq,
                kind=kind,
                user_id=user_id,
                state="queued",
                message=message,
                url=url,
                status=status,
                status_chat_id=user_id,
            )
        return seq

    def enqueue(self, job: _Job) -> None:
        if self._stopping:
            raise RuntimeError("pipeline is shutting down")
        self._reserve_disk_for_job(job)
        self._runtime_jobs[job.seq] = job
        self.input_q.put_nowait(job)
        self.active_seqs.add(job.seq)

    @staticmethod
    def _message_size_bytes(message: object) -> int:
        if message is None:
            return 0
        candidates = [
            getattr(getattr(message, "file", None), "size", None),
            getattr(getattr(message, "document", None), "size", None),
            getattr(getattr(getattr(message, "media", None), "document", None), "size", None),
        ]
        for value in candidates:
            try:
                if value is not None and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    def _estimate_job_bytes(self, job: _Job) -> int | None:
        if getattr(job, "cached_path", ""):
            try:
                return max(0, int(os.path.getsize(job.cached_path)))
            except OSError:
                pass
        if job.kind == "url" or getattr(job, "url", ""):
            return None
        messages = []
        if getattr(job, "album", None):
            messages.extend(job.album or [])
        elif getattr(job, "message", None) is not None:
            messages.append(job.message)
        sizes = [self._message_size_bytes(message) for message in messages]
        known = sum(size for size in sizes if size > 0)
        if messages and known > 0 and all(size > 0 for size in sizes):
            return known
        return None

    def _reserve_disk_for_job(self, job: _Job) -> None:
        manager = getattr(self, "disk", None)
        if manager is None:
            return
        decision = manager.reserve(job.seq, self._estimate_job_bytes(job))
        job.disk_reserved_bytes = decision.requested_bytes
        if not decision.healthy:
            logger.warning(
                "Job #%s disk preflight low headroom requested=%d available=%d free_after_pct=%.1f enforce=%s reason=%s",
                job.seq,
                decision.requested_bytes,
                decision.available_bytes,
                decision.free_percent,
                manager.enforce,
                decision.reason,
            )

    async def _ensure_disk_capacity(self, job: _Job) -> None:
        manager = getattr(self, "disk", None)
        if manager is None:
            return
        if manager.enforce and manager.max_cache_bytes and getattr(self, "job_queue", None) is not None:
            await self.job_queue.cleanup_to_waterline()
        decision = manager.reserve(job.seq, self._estimate_job_bytes(job))
        job.disk_reserved_bytes = decision.requested_bytes
        if decision.allowed:
            return
        if getattr(self, "job_queue", None) is not None and getattr(self, "repository", None) is not None:
            report = await self.job_queue.cleanup_to_waterline()
            if report["cleaned"] or report["failed"]:
                logger.info(
                    "Disk cleanup before job #%s cleaned=%d failed=%d freed=%d",
                    job.seq,
                    report["cleaned"],
                    report["failed"],
                    report["freed_bytes"],
                )
            decision = manager.reserve(job.seq, self._estimate_job_bytes(job))
            job.disk_reserved_bytes = decision.requested_bytes
        if not decision.allowed:
            manager.release(job.seq)
            raise OSError(errno.ENOSPC, "disk capacity gate")

    async def _run_download_with_capacity(self, job: _Job):
        await self._ensure_disk_capacity(job)
        return await self.downloader.run(job)

    async def recover_from_repository(self) -> list:
        """Repair durable jobs and recreate only transport-safe runtime objects."""
        if getattr(self, "repository", None) is None:
            return []
        released_cleanup = await self.repository.release_interrupted_disk_cleanup_claims()
        if released_cleanup:
            logger.warning("Released %d interrupted disk cleanup claims", released_cleanup)
        actions = await recover_jobs(self.repository)
        for action in actions:
            record = action.job
            if action.action == "failed":
                logger.warning(
                    "Recovery failed closed for DB job %s: %s",
                    record.id,
                    action.reason,
                )
                continue
            seq = int(record.legacy_seq if record.legacy_seq is not None else record.id)
            if getattr(self, "job_queue", None) is not None:
                self.job_queue.bind_recovered(seq, record.id)
            texts = await self.repository.list_job_texts(record.id)
            items = await self.repository.list_job_items(record.id)
            placeholders = []
            for item in items:
                caption = ""
                if item.metadata_json:
                    try:
                        caption = str(json.loads(item.metadata_json).get("caption") or "")
                    except Exception:
                        caption = ""
                placeholders.append(SimpleNamespace(message=caption))
            try:
                status = await self.client.send_message(
                    record.user_id,
                    f"♻️ 恢复任务 #{seq}："
                    f"{'等待重新下载' if action.action == 'download' else '缓存完整，等待发布'}",
                )
            except Exception:
                status = None
            if status is not None and getattr(self, "job_queue", None) is not None:
                await self.job_queue.set_status_reference(seq, status, chat_id=record.user_id)
            destination_snapshot = None
            if record.destination_profile_snapshot_json:
                try:
                    destination_snapshot = json.loads(record.destination_profile_snapshot_json)
                except Exception:
                    destination_snapshot = None
            job = _Job(
                seq=seq,
                kind=record.kind,
                status=status,
                message=(placeholders[0] if placeholders else SimpleNamespace(message="")),
                album=(placeholders if record.kind in {"album", "collection"} else None),
                url=record.source_url or "",
                spoiler=record.spoiler,
                user_id=record.user_id,
                texts=texts or None,
                destination_profile_id=record.destination_profile_id,
                destination_profile_snapshot=destination_snapshot,
            )
            self._runtime_jobs[seq] = job
            self.active_seqs.add(seq)
            if action.action == "download":
                self.input_q.put_nowait(job)
            else:
                self.jobs[seq] = job
                payload = list(action.paths)
                self._set_result(seq, payload[0] if len(payload) == 1 else payload)
            self._counter = max(self._counter, seq + 1)
        if actions:
            logger.info("Startup recovery scanned %d durable jobs", len(actions))
        return actions

    def _queue_position(self, seq: int) -> int:
        return 1 + sum(1 for s in self.active_seqs if s < seq)

    def task_label(self, seq: int) -> str:
        return f"队列第 {self._queue_position(seq)} 位"

    def _queue_view_state(self, user_id: int) -> QueueViewState:
        active = []
        for seq in sorted(self.active_seqs):
            info = self.active.get(seq) or {}
            if seq in self._download_tasks:
                state = "download"
            elif seq == self._uploading:
                state = "upload"
            elif seq in self._paused_files:
                state = "paused"
            elif seq in self.jobs:
                state = "ready"
            else:
                state = "queued"
            active.append(
                QueueItemView(
                    seq=seq,
                    position=self._queue_position(seq),
                    state=state,
                    pct=info.get("pct"),
                    item=info.get("item", 1),
                    items=info.get("items", 1),
                )
            )
        pending = tuple(
            PendingQueueItemView(
                seq=seq,
                position=index,
                kind=self.pending[seq].kind,
            )
            for index, seq in enumerate(sorted(self.pending), start=1)
        )
        return QueueViewState(
            show_progress=self._show_progress(user_id),
            active=tuple(active),
            pending=pending,
            has_sessions=bool(self.sessions),
            session_media=sum(s.media_count for s in self.sessions.values()),
            session_texts=sum(s.text_count for s in self.sessions.values()),
        )

    def register_pending(
        self,
        seq: int,
        kind: str,
        message: object = None,
        album: list = None,
        user_id: int = 0,
        texts: list = None,
    ) -> None:
        profiles = getattr(self, "destination_profiles", None)
        profile = profiles.current_profile if profiles is not None else None
        self.pending[seq] = _PendingJob(
            seq=seq, kind=kind, message=message, album=album, user_id=user_id,
            texts=texts,
            destination_profile_id=(profile.id if profile is not None else None),
            destination_profile_name=(profile.name if profile is not None else ""),
            destination_profile_snapshot=(profiles.current_snapshot() if profiles is not None else None),
        )
        self.pending[seq].timeout_task = asyncio.get_running_loop().create_task(
            self._confirm_timeout(seq)
        )

    def set_pending_status(self, seq: int, status: object) -> None:
        if seq in self.pending:
            self.pending[seq].status = status

    def collect_album(self, grouped_id: int, message: object, chat_id: int) -> None:
        buf = self.albums.get(chat_id)
        if buf is None:
            buf = _AlbumBuffer(
                chat_id=chat_id, messages=[], grouped_ids=set()
            )
            self.albums[chat_id] = buf
        buf.grouped_ids.add(grouped_id)
        if not any(m.id == message.id for m in buf.messages):
            buf.messages.append(message)
        if buf.task is not None:
            buf.task.cancel()
        buf.task = asyncio.get_running_loop().create_task(self._finalize_album(buf))

    async def _finalize_album(self, buf: _AlbumBuffer) -> None:
        await asyncio.sleep(COLLECTION_GATHER_SECONDS)
        self.albums.pop(buf.chat_id, None)
        if SESSION_COLLECT:
            try:
                await self._session_add_batch(
                    buf.chat_id, list(buf.messages), buf.chat_id
                )
            except Exception as exc:
                logger.exception("Failed to add album batch to session: %s", exc)
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
        logger.info("Job #%s confirmation timeout, auto as normal", seq)
        try:
            await self._auto_enqueue(
                pending.kind,
                pending.message,
                pending.album,
                pending.user_id,
                force_normal=True,
                texts=pending.texts,
                reserved_seq=seq,
            )
        except Exception as exc:
            logger.exception("Auto-process timeout job failed: %s", exc)
            self._set_cancelled(seq)
        if pending.status is not None:
            try:
                await pending.status.delete()
            except Exception:
                pass

    async def _session_add_batch(
        self, user_id: int, messages: list, chat_id: int = 0
    ) -> None:
        """把一批媒体加入合集会话（自动 /begin）：追加 → 会话状态消息首次创建一次，后续不再弹出。"""
        session = self.sessions.get(user_id)
        if session is None:
            session = _Session(user_id=user_id)
            self.sessions[user_id] = session
            logger.info("Session auto-started for user %s", user_id)
        session.items.append(list(messages))
        await self._session_touch(user_id, session)

    async def _session_add_text(self, user_id: int, text: str, chat_id: int = 0) -> None:
        """会话期间的文字评论：按行收集（/end 时整合为封面 caption 与封面一起发送）。"""
        session = self.sessions.get(user_id)
        if session is None:
            return
        session.texts.append(text)
        logger.info(
            "Session text #%d collected for user %s (%d chars)", len(session.texts), user_id, len(text)
        )
        await self._session_touch(user_id, session)

    async def _session_touch(self, user_id: int, session: _Session) -> None:
        """合集状态消息：仅在会话开始（首个媒体/评论）时创建一次。

        后续转发/评论**不再更新或弹出**，避免刷屏——保持一条固定消息，
        等用户发 /end 或点「🛑 结束并发布（/end）」按钮结束。
        """
        if session.status is not None:
            return
        summary = f"已收录 {len(session.items)} 项（{session.media_count} 个媒体"
        if session.texts:
            summary += f" · {session.text_count} 条评论"
        summary += "）"
        text = (
            f"📦 合集会话进行中 · {summary}\n"
            "继续转发自动加入合集，点下方按钮或发 /end 结束并发布"
        )
        buttons = [[Button.inline("🛑 结束并发布（/end）", f"session_end:{user_id}")]]
        try:
            session.status = await self.client.send_message(
                user_id, text, buttons=buttons
            )
        except Exception as exc:
            logger.exception("Session status update failed: %s", exc)

    async def _session_finalize(self, user_id: int, chat_id: int = 0) -> int:
        """结束会话：先收拢仍在聚合中的相册缓冲，再统一 18+ 询问/入队为单个 collection 任务。"""
        session = self.sessions.pop(user_id, None)
        if session is None:
            return 0
        if session.button_task is not None:
            session.button_task.cancel()
        items = list(session.items)
        buf = self.albums.pop(user_id, None)
        if buf is not None:
            if buf.task is not None:
                buf.task.cancel()
            if buf.messages:
                items.append(list(buf.messages))
                logger.info(
                    "Session finalize flushed pending album (%d msgs)", len(buf.messages)
                )
        flat = [m for item in items for m in item]
        if not flat:
            logger.info("Session for user %s finalized with no media", user_id)
            return 0
        if session.status is not None:
            try:
                await session.status.edit("🛑 已结束收集，正在处理…", buttons=None)
            except Exception:
                pass
        peer = chat_id or user_id
        texts = list(session.texts) or None
        try:
            if self._spoiler_mode(user_id) == "ask":
                seq = self.reserve_seq()
                await self._show_ask(
                    seq, "collection", None, flat, user_id, peer, texts=texts
                )
            else:
                await self._auto_enqueue(
                    "collection", None, flat, user_id, texts=texts
                )
        except Exception as exc:
            logger.exception("Session finalize enqueue failed: %s", exc)
            self.sessions[user_id] = session
            if buf is not None:
                self.albums[user_id] = buf
            raise
        logger.info(
            "Session for user %s finalized: %d items, %d media, %d texts",
            user_id,
            len(items),
            len(flat),
            len(texts or []),
        )
        return len(flat)

    async def _download_worker(self) -> None:
        while True:
            if self._stopping:
                return
            while self._paused:
                await asyncio.sleep(1)
            wake_job = await self.input_q.get()
            if self._stopping:
                self.input_q.task_done()
                return
            job = wake_job
            if getattr(self, "repository", None) is not None and getattr(self, "job_queue", None) is not None:
                await self.job_queue.drain_shadow()
                owner = f"download:{id(asyncio.current_task())}"
                claim = await self.job_queue.claim_next_download(owner)
                if claim is None:
                    self.input_q.task_done()
                    await asyncio.sleep(0.05)
                    continue
                seq = int(claim.job.legacy_seq if claim.job.legacy_seq is not None else claim.job.id)
                job = self._runtime_jobs.get(seq)
                if job is None:
                    logger.error("Claimed DB job %s has no runtime transport object", claim.job.id)
                    current = await self.repository.get_job(claim.job.id)
                    if current is not None:
                        await self.repository.transition_job(
                            current.id,
                            expected_revision=current.revision,
                            to_state="failed",
                            event_type="runtime_binding_missing",
                            payload={"schema_version": 1},
                        )
                    self.input_q.task_done()
                    continue
                self._repo_download_claimed.add(job.seq)
                self._repo_claim_owners[("download", job.seq)] = owner
            self.album_jobs.pop(job.seq, None)
            if self.pending_albums.get(job.user_id) == job.seq:
                del self.pending_albums[job.user_id]
            job.started = True
            if job.seq in self._cancel_marked:
                self._cancel_marked.discard(job.seq)
                await self._delete_status(job)
                self.input_q.task_done()
                continue
            self.jobs[job.seq] = job
            self._active_downloads += 1
            try:
                retries = 0
                while True:
                    if job.seq in self._cancel_marked:
                        logger.info("Job #%s cancelled during retry backoff", job.seq)
                        self._cancel_marked.discard(job.seq)
                        self._set_cancelled(job.seq)
                        await self._delete_status(job)
                        if getattr(self, "repository", None) is not None:
                            self.job_queue.shadow_transition(job.seq, "cancelled", "cancelled")
                            self._finish_seq(job.seq)
                        break
                    network_generation = self.network.generation
                    task = asyncio.get_running_loop().create_task(
                        self._run_download_with_capacity(job)
                    )
                    self._download_tasks[job.seq] = task
                    done, _ = await asyncio.wait({task}, timeout=DOWNLOAD_TIMEOUT)
                    if task in done:
                        try:
                            path = task.result()
                        except asyncio.CancelledError:
                            if self._stopping:
                                logger.info("Job #%s download interrupted by shutdown", job.seq)
                            else:
                                logger.info(
                                    "Job #%s download stopped by user", job.seq
                                )
                                self._cancel_marked.discard(job.seq)
                                self._set_cancelled(job.seq)
                                await self._delete_status(job)
                                if getattr(self, "repository", None) is not None:
                                    self.job_queue.shadow_transition(job.seq, "cancelled", "cancelled")
                                    self._finish_seq(job.seq)
                        except Exception as exc:
                            error = classify_error(exc, stage="download")
                            decision = self._retry_policy.decide(
                                error,
                                stage="download",
                                attempt=retries + 1,
                                now=time.time(),
                            )
                            if decision.should_retry:
                                switched = False
                                reevaluate = False
                                if error.code in {ErrorCode.NETWORK_TIMEOUT, ErrorCode.NETWORK_UNREACHABLE}:
                                    switch = await self.network.auto_switch(
                                        observed_generation=network_generation,
                                    )
                                    switched = switch.switched
                                    reevaluate = switch.reevaluate
                                    if switched:
                                        logger.info(
                                            "Job #%s 网络失败，已自动切换代理 #%s",
                                            job.seq,
                                            switch.index,
                                        )
                                    elif reevaluate:
                                        logger.info(
                                            "Job #%s 检测到网络代际更新，直接在新连接重试",
                                            job.seq,
                                        )
                                retries = decision.attempt
                                if getattr(self, "repository", None) is not None:
                                    await self.job_queue.record_retry(
                                        job.seq,
                                        phase="download",
                                        error_code=error.code.value,
                                        error_message=error.summary,
                                        retry_count=retries,
                                        next_retry_at=float(decision.next_retry_at),
                                    )
                                logger.warning(
                                    "Job #%s download failed code=%s, auto-retry %d/%d in %.1fs%s",
                                    job.seq,
                                    error.code.value,
                                    retries,
                                    decision.budget,
                                    float(decision.delay_seconds),
                                    "（已切换代理）"
                                    if switched
                                    else ("（网络已更新）" if reevaluate else ""),
                                )
                                await self._wait_retry(job.seq, float(decision.delay_seconds))
                                continue
                            logger.error(
                                "Download failed job=%s phase=download code=%s exception_type=%s traceback=%s",
                                job.seq,
                                error.code.value,
                                exc.__class__.__name__,
                                safe_traceback(exc),
                            )
                            if getattr(self, "repository", None) is not None:
                                await self._reply_error(
                                    job.seq,
                                    f"下载失败：{error.summary}",
                                    retry_job=job,
                                    exc=exc,
                                    phase="download",
                                    retry_count=retries,
                                )
                                self._finish_seq(job.seq)
                            else:
                                self._set_exception(job.seq, exc)
                        else:
                            self._set_result(job.seq, path)
                        break
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        if not getattr(job, "url_stage", ""):
                            logger.exception("Downloader task failed while settling timeout for job #%s", job.seq)
                    timeout_stage = getattr(job, "url_stage", "download")
                    timeout_label = "合并音视频" if timeout_stage == "postprocessing" else "下载"
                    timeout_error = classify_error(TimeoutError(timeout_label), stage="download")
                    decision = self._retry_policy.decide(
                        timeout_error,
                        stage="download",
                        attempt=retries + 1,
                        now=time.time(),
                    )
                    if decision.should_retry:
                        switch = None
                        if timeout_stage != "postprocessing":
                            switch = await self.network.auto_switch(
                                observed_generation=network_generation,
                            )
                        retries = decision.attempt
                        if getattr(self, "repository", None) is not None:
                            await self.job_queue.record_retry(
                                job.seq,
                                phase="download",
                                error_code=timeout_error.code.value,
                                error_message=timeout_error.summary,
                                retry_count=retries,
                                next_retry_at=float(decision.next_retry_at),
                            )
                        logger.warning(
                            "Job #%s %s timeout, auto-retry %d/%d in %.1fs%s",
                            job.seq,
                            timeout_label,
                            retries,
                            decision.budget,
                            float(decision.delay_seconds),
                            "（已切换代理）"
                            if switch is not None and switch.switched
                            else (
                                "（网络已更新）"
                                if switch is not None and switch.reevaluate
                                else ""
                            ),
                        )
                        await self._wait_retry(job.seq, float(decision.delay_seconds))
                        continue
                    logger.error(
                        "Job #%s %s timed out after %ss", job.seq, timeout_label, DOWNLOAD_TIMEOUT
                    )
                    if getattr(self, "repository", None) is not None:
                        await self._reply_error(
                            job.seq,
                            f"{timeout_label}超时（{DOWNLOAD_TIMEOUT} 秒）",
                            retry_job=job,
                            exc=TimeoutError(timeout_label),
                            phase="download",
                            retry_count=retries,
                        )
                        self._finish_seq(job.seq)
                    else:
                        self._set_exception(
                            job.seq,
                            TimeoutError(f"{timeout_label}超时（{DOWNLOAD_TIMEOUT} 秒）"),
                        )
                    break
            except asyncio.CancelledError:
                logger.info("Job #%s download worker cancelled", job.seq)
                if not self._stopping:
                    self._cancel_marked.discard(job.seq)
                    self._set_cancelled(job.seq)
                    await self._delete_status(job)
                if getattr(self, "repository", None) is not None:
                    self.job_queue.shadow_transition(job.seq, "cancelled", "cancelled")
                    self._finish_seq(job.seq)
            finally:
                self._download_tasks.pop(job.seq, None)
                self._repo_download_claimed.discard(job.seq)
                self._repo_claim_owners.pop(("download", job.seq), None)
                self._active_downloads = max(0, self._active_downloads - 1)
                self.input_q.task_done()

    async def _wait_retry(self, seq: int, delay_seconds: float) -> bool:
        """Wait for backoff or return early when the job is cancelled."""
        if seq in self._cancel_marked:
            return True
        interrupt = asyncio.Event()
        self._retry_interrupts[seq] = interrupt
        if seq in self._cancel_marked:
            interrupt.set()
        sleeper = asyncio.create_task(self._retry_sleep(max(0.0, delay_seconds)))
        cancelled = asyncio.create_task(interrupt.wait())
        try:
            done, _ = await asyncio.wait(
                {sleeper, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            return cancelled in done and bool(cancelled.result())
        finally:
            for task in (sleeper, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, cancelled, return_exceptions=True)
            if self._retry_interrupts.get(seq) is interrupt:
                self._retry_interrupts.pop(seq, None)

    async def _on_pre_download(self, job) -> None:
        self._progress_tracker.begin_phase(job.seq, "downloading")
        if getattr(self, "job_queue", None) is not None and job.seq not in self._repo_download_claimed:
            self.job_queue.shadow_transition(job.seq, "download_started", "downloading")
        text, buttons = self._job_card(job, "downloading")
        await self._safe_edit(job, text, buttons=buttons)

    async def _on_download_progress(
        self, seq: int, received: int, total: int, item: int, items: int
    ) -> None:
        job = self.jobs.get(seq)
        if job is None:
            return
        if self._progress_tracker.phase_is_stale(seq, "downloading"):
            return
        owner = self._repo_claim_owners.get(("download", seq))
        if owner and getattr(self, "job_queue", None) is not None:
            await self.job_queue.heartbeat_claim(seq, "download", owner)
        progress = self._progress_tracker.update(
            seq, "downloading", received, total, item, items
        )
        overall = progress.pct
        self.active[seq] = {
            "phase": "download",
            "pct": overall,
            "item": item,
            "items": items,
            "user_id": job.user_id,
        }
        if (
            getattr(self, "repository", None) is not None
            and getattr(self, "job_queue", None) is not None
            and self._progress_tracker.allow_db(progress)
        ):
            await self.job_queue.persist_progress(
                seq,
                received=received,
                total=total,
                item=item,
                items=items,
            )
        logger.info(
            "Job #%s download progress: %d/%d (%d%%)", seq, received, total, overall
        )
        await self._update_progress_status(seq)

    async def _on_download_status(self, job, status: str) -> None:
        if status != "postprocessing":
            return
        if self._progress_tracker.phase_is_stale(job.seq, "downloading"):
            return
        await self._safe_edit(
            job,
            f"🧩 任务 #{job.seq} · 正在合并音视频\n──────────\nyt-dlp 后处理进行中，请稍候…",
            buttons=[Button.inline("✖️ 取消", f"stop:{job.seq}")],
        )

    async def _on_download_done(self, job, paths) -> None:
        self._progress_tracker.begin_phase(job.seq, "ready")
        if getattr(self, "job_queue", None) is not None:
            file_list = [paths] if isinstance(paths, str) else list(paths or [])
            if getattr(self, "repository", None) is not None:
                await self.job_queue.complete_download(job.seq, file_list)
            else:
                self.job_queue.shadow_download_completed(job.seq, file_list)
        text, buttons = self._job_card(job, "ready", payload=paths)
        await self._safe_edit(job, text, buttons=buttons)

    async def _on_media_compat(self, job, paths):
        result = await self.media_compat.process(job, paths)
        if result is None:
            return None
        if getattr(self, "repository", None) is not None:
            values = result if isinstance(result, list) else [result]
            metadata = getattr(job, "_media_metadata", {})
            payloads = []
            for path in values:
                info = metadata.get(os.path.realpath(path))
                payloads.append(
                    None
                    if info is None
                    else {
                        "container": info.container,
                        "video_codec": info.video_codec,
                        "audio_codec": info.audio_codec,
                        "duration_seconds": info.duration_seconds,
                        "width": info.width,
                        "height": info.height,
                        "rotation": info.rotation,
                        "bitrate": info.bitrate,
                        "stream_count": info.stream_count,
                        "faststart": info.faststart,
                        "streaming_ready": info.telegram_streaming_ready,
                    }
                )
            try:
                await self.repository.set_job_item_media_metadata(job.seq, payloads)
            except Exception as exc:
                logger.warning("Job #%s M1 metadata persistence skipped: %s", job.seq, exc.__class__.__name__)
        return result

    async def _on_dedup_hash(self, job, paths) -> None:
        manager = getattr(self, "dedup_manager", None)
        if manager is None or getattr(self, "repository", None) is None:
            return
        try:
            hashes = await manager.hash_job_paths(job.seq, paths)
            if hashes:
                job._content_hashes = {os.path.realpath(item.path): item for item in hashes}
                logger.info("Job #%s D1 hashed %d media file(s)", job.seq, len(hashes))
        except Exception as exc:
            # D1 indexing is an optimization. It must never break the publish path.
            logger.warning("Job #%s D1 hashing skipped: %s", job.seq, exc.__class__.__name__)

    async def _on_webdav_upload(self, job, paths) -> None:
        """下载完成后后台上传到 WebDAV，不阻塞主流程。

        目录结构：<路径>/<当天日期>/<当天第 N 次上传>/（N 持久化，重启不重置）。
        逐文件记录状态到 webdav_logs（批次开始即落盘，重启可见），失败文件保留本地缓存供重试。
        发送一条状态消息实时展示上传进度（逐文件更新，最后编辑为最终结果）。
        """
        cfg = self.webdav_cfg
        if not cfg.get("enabled") or not cfg.get("url"):
            return
        file_list = [paths] if isinstance(paths, str) else list(paths or [])
        file_list = [p for p in file_list if os.path.isfile(p)]
        if not file_list:
            return
        date_str = datetime.now().strftime("%Y-%m-%d")
        async with self._webdav_count_lock:
            n = self.webdav_count.get(date_str, 0) + 1
            self.webdav_count[date_str] = n
            self._save_webdav_count()
        remote_dir = (
            f"{str(cfg.get('path', '')).strip('/')}/{date_str}/{n}"
        )
        seq = job.seq
        log = {
            "key": f"{seq}:{int(time.time())}",
            "seq": seq,
            "user_id": job.user_id,
            "ts": time.time(),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "remote_dir": remote_dir,
            "files": [],
        }
        content_hashes = getattr(job, "_content_hashes", {})
        for p in file_list:
            stem, ext = os.path.splitext(os.path.basename(p))
            content_hash = content_hashes.get(os.path.realpath(p))
            md5_short = getattr(content_hash, "md5_short", "") or _file_md5_short(p)
            log["files"].append(
                {
                    "name": f"{md5_short}{ext}",
                    "local": p,
                    "status": "pending",
                }
            )

        # 批次开始即落盘（进行中在 /webdav 记录里可见，重启也能恢复）
        self.webdav_logs[log["key"]] = log
        self._save_webdav_logs()
        total = len(log["files"])
        status_msg = None
        if job.user_id:
            try:
                status_msg = await self.client.send_message(
                    job.user_id,
                    f"📤 WebDAV 开始备份：{total} 个文件\n"
                    f"────────────────────────\n"
                    f"📂 {remote_dir}\n"
                    f"{render_bar(0)}  0%",
                )
            except Exception as exc:
                logger.warning("WebDAV 开始通知发送失败: %s", exc)

        async def _render_final() -> str:
            """渲染最终结果文本（全部成功 / 有失败）。"""
            ok = sum(1 for f in log["files"] if f.get("status") == "ok")
            failed = total - ok
            if failed == 0:
                return (
                    f"✅ WebDAV 备份完成：{ok} 个文件\n"
                    f"────────────────────────\n"
                    f"{render_bar(100)}  {ok}/{total}\n"
                    f"📂 {remote_dir}"
                )
            return (
                f"⚠️ WebDAV 备份：{failed}/{total} 个文件失败\n"
                f"────────────────────────\n"
                f"{render_bar(ok / total * 100 if total else 0)}  {ok}/{total}\n"
                f"📂 {remote_dir}\n"
                f"失败文件将每小时自动补传，也可点按钮立即重试"
            )

        async def _edit_status(text: str, buttons=None) -> None:
            nonlocal status_msg
            if not status_msg:
                return
            try:
                status_msg = await status_msg.edit(text, buttons=buttons)
            except Exception as exc:
                logger.debug("WebDAV 状态消息编辑失败: %s", exc)

        # 进度编辑节流 + 代际计数：防止迟到的进度编辑覆盖最终结果
        state = {"gen": 0, "last": 0.0}
        loop = asyncio.get_running_loop()

        def _progress_cb(sent: int, size: int, _fname: str = "", _cur: int = 1) -> None:
            now = time.monotonic()
            if now - state["last"] < 5.0:
                return
            state["last"] = now
            gen = state["gen"]
            pct = (sent / size * 100) if size else 0

            async def _apply() -> None:
                if state["gen"] != gen:
                    return
                await _edit_status(
                    f"📤 WebDAV 备份中 {_cur}/{total}\n"
                    f"{render_bar(pct)}  {pct:.0f}%\n"
                    f"{_fname}\n"
                    f"📂 {remote_dir}"
                )

            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_apply()))

        async def _upload() -> None:
            try:
                for f in log["files"]:
                    if f["status"] == "ok":
                        continue
                    f["status"] = "uploading"
                    self._save_webdav_logs()
                    cur = sum(1 for x in log["files"] if x.get("status") == "ok") + 1
                    await _edit_status(
                        f"📤 WebDAV 备份中 {cur}/{total}\n"
                        f"{render_bar(0)}  0%\n"
                        f"{f['name']}\n"
                        f"📂 {remote_dir}"
                    )
                    ok = await self._webdav_transfer_file(
                        seq=seq,
                        remote_dir=remote_dir,
                        file=f,
                        cfg=cfg,
                        progress_callback=lambda s, sz, _n=f["name"], _c=cur: _progress_cb(s, sz, _n, _c),
                    )
                    self._save_webdav_logs()
                    logger.info(
                        "Job #%s webdav %s -> %s (%s)",
                        seq,
                        f["name"],
                        remote_dir,
                        "OK" if ok else "FAILED",
                    )
            except Exception as exc:
                logger.error("Job #%s webdav upload error: %s", seq, exc)
                for f in log["files"]:
                    if f["status"] == "pending":
                        f["status"] = "failed"
                self._save_webdav_logs()
            finally:
                await self._webdav_finalize_attempt(seq, remote_dir, log["files"])
                failed = [f for f in log["files"] if f["status"] == "failed"]
                if failed:
                    self.webdav_keep_cache.add(seq)
                else:
                    self.webdav_keep_cache.discard(seq)
                self.webdav_logs[log["key"]] = log
                self._save_webdav_logs()
                final_text = await _render_final()
                if status_msg:
                    buttons = None
                    if failed:
                        buttons = [[Button.inline("🔄 立即重试", f"wd_retry:{log['key']}")]]
                    state["gen"] += 1
                    await _edit_status(final_text, buttons=buttons)
                else:
                    await self._notify_webdav_result(job.user_id, log)

        task = asyncio.get_running_loop().create_task(_upload())
        self._webdav_tasks[task] = seq

        def _done(t: asyncio.Task) -> None:
            self._webdav_tasks.pop(t, None)
            if t.exception() and not isinstance(t.exception(), asyncio.CancelledError):
                logger.error("Job #%s webdav task failed", seq)

        task.add_done_callback(_done)

    async def _webdav_transfer_file(
        self,
        *,
        seq: int,
        remote_dir: str,
        file: dict,
        cfg: dict | None = None,
        progress_callback=None,
    ) -> bool:
        """Upload one WebDAV file with centralized backup retry semantics."""
        cfg = self.webdav_cfg if cfg is None else cfg
        local = str(file.get("local") or "")
        name = str(file.get("name") or os.path.basename(local))
        size = os.path.getsize(local) if local and os.path.isfile(local) else int(file.get("size") or 0)
        file["size"] = size
        attempt = durable_file = None
        if getattr(self, "backup_manager", None) is not None and getattr(self, "repository", None) is not None:
            attempt, durable_file = await self.backup_manager.ensure_file(
                seq, remote_dir, {**file, "local": local, "name": name, "size": size}
            )

        async def persist_file(state: str, *, error=None, bytes_done=None) -> None:
            if durable_file is None:
                return
            await self.backup_manager.update_file(
                durable_file.id,
                state=state,
                bytes_done=bytes_done,
                error_code=error.code.value if error is not None else None,
                error_message=error.summary if error is not None else None,
            )

        if not local or not os.path.isfile(local):
            file.update(
                status="failed",
                error_code=ErrorCode.CACHE_MISSING.value,
                error_message="备份所需的本地缓存不存在",
                next_retry_at=None,
            )
            if durable_file is not None:
                await self.backup_manager.update_file(
                    durable_file.id,
                    state="failed",
                    bytes_done=0,
                    error_code=ErrorCode.CACHE_MISSING.value,
                    error_message="备份所需的本地缓存不存在",
                )
            if attempt is not None:
                await self.backup_manager.update_attempt(
                    attempt.id,
                    state="failed",
                    retry_count=int(file.get("retry_count") or 0),
                    next_retry_at=None,
                    error_code=ErrorCode.CACHE_MISSING.value,
                    error_message="备份所需的本地缓存不存在",
                    finished=True,
                )
            return False

        async def persist_attempt(
            state: str,
            *,
            retry_count: int | None = None,
            next_retry_at: float | None = None,
            error=None,
            finished: bool = False,
        ) -> None:
            if attempt is None:
                return
            await self.backup_manager.update_attempt(
                attempt.id,
                state=state,
                retry_count=retry_count,
                next_retry_at=next_retry_at,
                error_code=error.code.value if error is not None else None,
                error_message=error.summary if error is not None else None,
                finished=finished,
            )

        remote_size = await asyncio.to_thread(
            webdav.remote_file_size,
            cfg.get("url"), remote_dir, name, cfg.get("user"), cfg.get("pass"),
        )
        if remote_size is not None and remote_size == size:
            file.update(status="ok", error_code=None, error_message=None,
                        retry_count=int(file.get("retry_count") or 0), next_retry_at=None)
            await persist_file("succeeded", bytes_done=size)
            return True

        budget = max(0, int(cfg.get("retry", 2) or 0))
        policy = RetryPolicy(budgets={"backup": budget})
        retries = max(0, int(file.get("retry_count") or 0))
        while True:
            file["status"] = "uploading"
            file["next_retry_at"] = None
            await persist_file("uploading", bytes_done=0)
            await persist_attempt("running", retry_count=retries)
            try:
                uploaded = await asyncio.to_thread(
                    webdav.upload_once,
                    cfg.get("url"), remote_dir, local,
                    cfg.get("user"), cfg.get("pass"),
                    remote_name=name, progress_callback=progress_callback,
                )
                if not uploaded:
                    raise webdav.WebDavUploadError("webdav upload was not confirmed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = classify_error(exc, stage="backup")
                decision = policy.decide(
                    error, stage="backup", attempt=retries + 1, now=time.time()
                )
                file.update(
                    status="failed",
                    error_code=error.code.value,
                    error_message=error.summary,
                )
                await persist_file("failed", error=error)
                if decision.should_retry:
                    retries = decision.attempt
                    file["retry_count"] = retries
                    file["next_retry_at"] = float(decision.next_retry_at)
                    await persist_attempt(
                        "retry_wait", retry_count=retries,
                        next_retry_at=float(decision.next_retry_at), error=error,
                    )
                    logger.warning(
                        "Job #%s webdav %s failed code=%s, retry %d/%d in %.1fs",
                        seq, name, error.code.value, retries, decision.budget,
                        float(decision.delay_seconds),
                    )
                    await self._retry_sleep(float(decision.delay_seconds))
                    continue
                file["retry_count"] = retries
                file["next_retry_at"] = None
                await persist_attempt("failed", retry_count=retries, error=error)
                logger.error(
                    "Job #%s webdav %s terminal failure code=%s exception_type=%s traceback=%s",
                    seq, name, error.code.value, exc.__class__.__name__, safe_traceback(exc),
                )
                return False
            file.update(
                status="ok", error_code=None, error_message=None,
                retry_count=retries, next_retry_at=None,
            )
            await persist_file("succeeded", bytes_done=size)
            return True

    async def _webdav_finalize_attempt(self, seq: int, remote_dir: str, files: list[dict]) -> None:
        if getattr(self, "backup_manager", None) is None or getattr(self, "repository", None) is None:
            return
        seed = next((f for f in files if f.get("local") and f.get("name")), None)
        if seed is None:
            return
        attempt, _ = await self.backup_manager.ensure_file(seq, remote_dir, seed)
        if attempt is None:
            return
        failed = [f for f in files if f.get("status") != "ok"]
        if failed:
            first = failed[0]
            code = first.get("error_code") or ErrorCode.UNKNOWN.value
            summary = first.get("error_message") or "WebDAV 备份失败"
            await self.backup_manager.update_attempt(
                attempt.id, state="failed",
                retry_count=max((int(f.get("retry_count") or 0) for f in files), default=0),
                next_retry_at=min(
                    (float(f["next_retry_at"]) for f in failed if f.get("next_retry_at") is not None),
                    default=None,
                ),
                error_code=code, error_message=summary, finished=True,
            )
        else:
            await self.backup_manager.update_attempt(
                attempt.id, state="succeeded",
                retry_count=max((int(f.get("retry_count") or 0) for f in files), default=0),
                next_retry_at=None, error_code=None, error_message=None, finished=True,
            )

    async def _notify_webdav_result(self, user_id: int, log: dict) -> None:
        """上传批次结束后通知用户结果（成功/失败）。"""
        if not user_id:
            return
        files = log.get("files", [])
        total = len(files)
        failed = [f for f in files if f.get("status") == "failed"]
        remote = log.get("remote_dir", "")
        try:
            if not failed:
                await self.client.send_message(
                    user_id,
                    f"✅ WebDAV 备份完成：{total} 个文件\n{remote}",
                )
            else:
                await self.client.send_message(
                    user_id,
                    f"⚠️ WebDAV 备份：{len(failed)}/{total} 个文件失败\n{remote}\n"
                    f"将每小时自动补传，也可点按钮立即重试",
                    buttons=[
                        [
                            Button.inline(
                                "🔄 立即重试", f"wd_retry:{log.get('key', '')}"
                            )
                        ]
                    ],
                )
        except Exception as exc:
            logger.warning("WebDAV 结果通知发送失败: %s", exc)

    async def _notify_webdav_autoretry_done(self, log: dict) -> None:
        """自动补传最终全部成功时通知用户。"""
        user_id = log.get("user_id")
        if not user_id:
            return
        try:
            await self.client.send_message(
                user_id,
                f"✅ WebDAV 已自动补传完成：{len(log.get('files', []))} 个文件\n"
                f"{log.get('remote_dir', '')}",
            )
        except Exception as exc:
            logger.warning("WebDAV 补传通知失败: %s", exc)

    def _wd_cfg_lines(self, cfg: dict) -> list:
        """WebDAV 配置字段行（主视图与编辑页共享），前缀定宽对齐。"""
        return render_webdav_cfg_lines(self._webdav_cfg_state(cfg))

    def _webdav_cfg_state(self, cfg: dict | None = None) -> WebDavConfigViewState:
        cfg = self.webdav_cfg if cfg is None else cfg
        return WebDavConfigViewState(
            enabled=bool(cfg.get("enabled")),
            url=cfg.get("url") or "",
            user=cfg.get("user") or "",
            has_password=bool(cfg.get("pass")),
            path=cfg.get("path") or "",
            retry=cfg.get("retry"),
            backup_policy=str(cfg.get("backup_policy") or "best_effort"),
        )

    def _webdav_cfg_view(self) -> tuple:
        """按钮式配置主视图（/webdav）：状态卡片 + 3 个入口按钮，避免臃肿。"""
        return render_webdav_cfg_view(self._webdav_cfg_state())

    def _webdav_cfg_fields_view(self) -> tuple:
        """「修改配置」字段页：一次可连续修改多个字段，完成后返回。"""
        return render_webdav_cfg_fields_view(self._webdav_cfg_state())

    def _webdav_cache_dirs(self) -> list:
        """扫描 downloads/ 下的 job-* 目录，找出仍含文件的待上传缓存。

        排除：空目录、已被 webdav_logs 全部 ok 覆盖（本地缓存已确认上传）的目录。
        返回 [(job_dir, seq, files_count, total_size, remote_dir)]。
        """
        result = []
        try:
            entries = sorted(os.listdir(DOWNLOAD_DIR))
        except OSError:
            return result
        for name in entries:
            if not name.startswith("job-"):
                continue
            seq_s = name[4:]
            if not seq_s.isdigit():
                continue
            job_dir = os.path.join(DOWNLOAD_DIR, name)
            if not os.path.isdir(job_dir):
                continue
            files = [
                f for f in os.listdir(job_dir)
                if os.path.isfile(os.path.join(job_dir, f))
            ]
            if not files:
                continue
            # 已被某条 log 全部 ok 覆盖则不算待上传
            all_ok = False
            remote_dir = ""
            for log in self.webdav_logs.values():
                if log.get("seq") != int(seq_s):
                    continue
                remote_dir = log.get("remote_dir", "")
                ls = log.get("files", [])
                if ls and all(f.get("status") == "ok" for f in ls):
                    all_ok = True
                break
            if all_ok:
                continue
            # 无 log 记录时按目录 mtime 日期 + webdav_count 序号推导 remote_dir
            if not remote_dir:
                try:
                    mtime = os.path.getmtime(job_dir)
                    d = datetime.fromtimestamp(mtime)
                    date_str = d.strftime("%Y-%m-%d")
                    n = self.webdav_count.get(date_str, 1)
                    remote_dir = (
                        f"{str(self.webdav_cfg.get('path', '')).strip('/')}"
                        f"/{date_str}/{n}"
                    )
                except Exception:
                    remote_dir = ""
            size = sum(
                os.path.getsize(os.path.join(job_dir, f))
                for f in files
                if os.path.isfile(os.path.join(job_dir, f))
            )
            result.append((job_dir, int(seq_s), len(files), size, remote_dir))
        return result

    def _webdav_cache_view(self) -> tuple:
        """「本地待上传缓存」区块：每个 job 目录一行 + 上传按钮。"""
        dirs = self._webdav_cache_dirs()
        lines = ["📦 本地待上传缓存", ""]
        buttons: list = []
        if not dirs:
            lines.append("（无待上传缓存）")
            return "\n".join(lines), buttons
        for index, (job_dir, seq, n, size, remote_dir) in enumerate(dirs):
            size_mb = size / (1024 * 1024)
            size_txt = f"{size_mb:.0f}M" if size_mb < 1024 else f"{size_mb / 1024:.1f}G"
            remote_txt = remote_dir or "（待定）"
            lines.append(
                f"{_pos_token(index + 1)} {os.path.basename(job_dir)}  "
                f"{n} 个文件 · {size_txt}"
            )
            lines.append(f"   📂 {remote_txt}")
            buttons.append(
                [Button.inline(f"📤 上传 → {os.path.basename(job_dir)}", f"wd_cache_up:{seq}")]
            )
        return "\n".join(lines), buttons

    def _webdav_logs_view(self) -> tuple:
        """/webdavlogs 视图：顶部本地待上传缓存 + 上传记录（分层清晰）。"""
        self._save_webdav_logs()
        cache_text, cache_buttons = self._webdav_cache_view()
        logs = sorted(
            self.webdav_logs.items(),
            key=lambda kv: kv[1].get("ts", 0),
            reverse=True,
        )
        lines = [cache_text]
        buttons: list = [*cache_buttons]
        lines.append("")
        lines.append("────────────────────────")
        lines.append("📁 上传记录（最近 24 小时）")
        if not logs:
            lines.append("")
            lines.append("（暂无记录）")
            return "\n".join(lines), buttons or [[Button.inline("🔄 刷新", "wd_cfg:logs")]]
        for index, (key, log) in enumerate(logs):
            files = log.get("files", [])
            total = len(files)
            ok = sum(1 for f in files if f.get("status") == "ok")
            running = any(
                f.get("status") in ("pending", "uploading") for f in files
            )
            pending = sum(
                1 for f in files if f.get("status") in ("pending", "uploading")
            )
            failed = total - ok - pending
            if running:
                mark = "⏳ 进行中"
                bar_pct = ok / total * 100 if total else 0
            elif failed == 0:
                mark = "✅ 全部成功"
                bar_pct = 100
            else:
                mark = f"⚠️ 失败 {failed}/{total}"
                bar_pct = ok / total * 100 if total else 0
            # 时间短格式：08-17 17:56（跨年才显示年份）
            t = log.get("time", "")
            t_short = t
            try:
                dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
                t_short = dt.strftime("%m-%d %H:%M")
                if dt.year != datetime.now().year:
                    t_short = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
            lines.append("")
            lines.append(f"{_pos_token(index + 1)} {t_short}  {mark}")
            lines.append(f"   📂 {log.get('remote_dir', '')}")
            lines.append(f"   {render_bar(bar_pct)}  {ok}/{total}")
            if failed > 0:
                first_failed = next((f for f in files if f.get("status") == "failed"), None)
                if first_failed is not None and first_failed.get("error_code"):
                    lines.append(f"   ⚠️ {first_failed.get('error_code')}")
                retry_at = min(
                    (float(f["next_retry_at"]) for f in files if f.get("next_retry_at") is not None),
                    default=None,
                )
                if retry_at is not None:
                    lines.append(
                        f"   ⏰ 自动重试：{datetime.fromtimestamp(retry_at).strftime('%m-%d %H:%M:%S')}"
                    )
            if running:
                continue
            row = []
            if failed > 0:
                row.append(Button.inline("🔄 重试", f"wd_retry:{key}"))
            row.append(Button.inline("🗑 删除", f"wd_del:{key}"))
            buttons.append(row)
        return "\n".join(lines), buttons

    async def _webdav_retry(self, key: str) -> str:
        """重试某条记录中失败的文件（从保留的本地缓存重传）。"""
        log = self.webdav_logs.get(key)
        if not log:
            return "记录不存在或已过期"
        failed = [f for f in log.get("files", []) if f.get("status") != "ok"]
        if not failed:
            return "没有失败的文件"
        cfg = self.webdav_cfg
        if not cfg.get("url"):
            return "WebDAV 未配置地址（先用 /webdav 配置）"
        async def _upload() -> None:
            try:
                for f in failed:
                    ok = await self._webdav_transfer_file(
                        seq=log["seq"], remote_dir=log["remote_dir"],
                        file=f, cfg=cfg,
                    )
                    logger.info(
                        "Job #%s webdav retry %s (%s)",
                        log["seq"],
                        f["name"],
                        "OK" if ok else "FAILED",
                    )
            except Exception as exc:
                logger.error("Job #%s webdav retry error: %s", log["seq"], exc)
            finally:
                await self._webdav_finalize_attempt(log["seq"], log["remote_dir"], log.get("files", []))
                if all(f.get("status") == "ok" for f in log.get("files", [])):
                    self.webdav_keep_cache.discard(log["seq"])
                    self._schedule_cleanup(log["seq"], "")
                self._save_webdav_logs()

        task = asyncio.get_running_loop().create_task(_upload())
        self._webdav_tasks[task] = log["seq"]

        def _done(t: asyncio.Task) -> None:
            self._webdav_tasks.pop(t, None)

        task.add_done_callback(_done)
        return f"🔄 正在重试 {len(failed)} 个文件…"

    async def _webdav_upload_cache(self, seq: int, user_id: int = 0) -> str:
        """补传本地缓存目录（/webdavlogs 的「📤 上传」按钮）。

        目录 = DOWNLOAD_DIR/job-<seq>；remote_dir 优先取该 seq 已有 log 记录，
        否则按 webdav_count 的当天序号推导 path/日期/N。逐文件上传（hash 名，
        PROPFIND 确认），成功后删除本地文件，批次结束写 log + 通知。
        幂等：上传前 PROPFIND 查重，远端已有同名且大小一致则跳过（防重复上传）。
        实时进度经 status_msg 逐文件更新（user_id 指定接收用户）。
        """
        job_dir = os.path.join(DOWNLOAD_DIR, f"job-{seq}")
        if not os.path.isdir(job_dir):
            return "❌ 缓存目录不存在"
        cfg = self.webdav_cfg
        if not cfg.get("enabled") or not cfg.get("url"):
            return "❌ WebDAV 未启用或未配置地址（先用 /webdav）"
        files = sorted(
            f for f in os.listdir(job_dir)
            if os.path.isfile(os.path.join(job_dir, f))
        )
        if not files:
            return "缓存目录为空，无需上传"

        # 推导 remote_dir：优先该 seq 已有 log；否则按当天第 N 次
        remote_dir = ""
        for log in self.webdav_logs.values():
            if log.get("seq") == seq and log.get("remote_dir"):
                remote_dir = log["remote_dir"]
                break
        if not remote_dir:
            date_str = datetime.now().strftime("%Y-%m-%d")
            async with self._webdav_count_lock:
                n = self.webdav_count.get(date_str, 0) + 1
                self.webdav_count[date_str] = n
                self._save_webdav_count()
            remote_dir = f"{str(cfg.get('path', '')).strip('/')}/{date_str}/{n}"

        log = {
            "key": f"{seq}:{int(time.time())}",
            "seq": seq,
            "user_id": user_id,
            "ts": time.time(),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "remote_dir": remote_dir,
            "files": [],
        }
        for p in files:
            full = os.path.join(job_dir, p)
            stem, ext = os.path.splitext(p)
            log["files"].append(
                {
                    "name": f"{_file_md5_short(full)}{ext}",
                    "local": full,
                    "status": "pending",
                }
            )
        self.webdav_logs[log["key"]] = log
        self._save_webdav_logs()

        total = len(log["files"])
        # 进度状态消息（user_id 有效才发；进行中不撤，结束后 10s 撤）
        status_msg = None
        if user_id:
            try:
                status_msg = await self.client.send_message(
                    user_id,
                    f"📤 WebDAV 开始备份：{total} 个文件\n"
                    f"────────────────────────\n"
                    f"📂 {remote_dir}\n"
                    f"{render_bar(0)}  0%",
                )
            except Exception as exc:
                logger.warning("WebDAV 开始通知发送失败: %s", exc)

        async def _edit_status(text: str) -> None:
            nonlocal status_msg
            if not status_msg:
                return
            try:
                status_msg = await status_msg.edit(text)
            except Exception as exc:
                logger.debug("WebDAV 状态消息编辑失败: %s", exc)

        state = {"gen": 0, "last": 0.0}
        loop = asyncio.get_running_loop()

        def _progress_cb(sent: int, size: int, _fname: str = "", _cur: int = 1) -> None:
            now = time.monotonic()
            if now - state["last"] < 5.0:
                return
            state["last"] = now
            gen = state["gen"]
            pct = (sent / size * 100) if size else 0

            async def _apply() -> None:
                if state["gen"] != gen:
                    return
                await _edit_status(
                    f"📤 WebDAV 备份中 {_cur}/{total}\n"
                    f"{render_bar(pct)}  {pct:.0f}%\n"
                    f"{_fname}\n"
                    f"📂 {remote_dir}"
                )

            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_apply()))

        for f in log["files"]:
            if f["status"] == "ok":
                continue
            f["status"] = "uploading"
            self._save_webdav_logs()
            cur = sum(1 for x in log["files"] if x.get("status") == "ok") + 1
            await _edit_status(
                f"📤 WebDAV 备份中 {cur}/{total}\n"
                f"{render_bar(0)}  0%\n"
                f"{f['name']}\n"
                f"📂 {remote_dir}"
            )
            ok = await self._webdav_transfer_file(
                seq=seq,
                remote_dir=remote_dir,
                file=f,
                cfg=cfg,
                progress_callback=lambda s, sz, _n=f["name"], _c=cur: _progress_cb(s, sz, _n, _c),
            )
            if ok:
                try:
                    os.remove(f["local"])
                except OSError as exc:
                    logger.warning("WebDAV 缓存删除失败 %s: %s", f["local"], exc)
            self._save_webdav_logs()
            logger.info(
                "Job #%s webdav cache upload %s -> %s (%s)",
                seq, f["name"], remote_dir, "OK" if ok else "FAILED",
            )
        failed = [f for f in log["files"] if f["status"] == "failed"]
        await self._webdav_finalize_attempt(seq, remote_dir, log["files"])
        if failed:
            self.webdav_keep_cache.add(seq)
        else:
            self.webdav_keep_cache.discard(seq)
            try:
                os.rmdir(job_dir)
            except OSError:
                pass
        self.webdav_logs[log["key"]] = log
        self._save_webdav_logs()

        # 最终结果：编辑进度消息为最终 + 10s 撤（进行中不撤策略）
        ok_n = total - len(failed)
        if status_msg:
            if not failed:
                final_text = (
                    f"✅ WebDAV 备份完成：{ok_n} 个文件\n"
                    f"────────────────────────\n"
                    f"{render_bar(100)}  {ok_n}/{total}\n"
                    f"📂 {remote_dir}"
                )
            else:
                final_text = (
                    f"⚠️ WebDAV 备份：{len(failed)}/{total} 个文件失败\n"
                    f"────────────────────────\n"
                    f"{render_bar(ok_n / total * 100 if total else 0)}  {ok_n}/{total}\n"
                    f"📂 {remote_dir}\n"
                    f"失败文件将每小时自动补传，也可点按钮立即重试"
                )
            state["gen"] += 1
            await _edit_status(final_text)
            if AUTO_DELETE_SECONDS > 0:
                asyncio.get_running_loop().create_task(
                    _delete_after(status_msg, AUTO_DELETE_SECONDS)
                )
        else:
            await self._notify_webdav_result(user_id, log)
        if failed:
            return f"⚠️ 缓存上传：{len(failed)}/{total} 个失败（将在 /webdavlogs 显示，可重试）"
        return f"✅ 缓存上传完成：{total} 个文件\n📂 {remote_dir}"

    async def _webdav_delete(self, key: str) -> str:
        """删除某条记录本次上传的所有远端文件（逐个 DELETE，不删目录）。"""
        log = self.webdav_logs.get(key)
        if not log:
            return "记录不存在或已过期"
        cfg = self.webdav_cfg
        if not cfg.get("url"):
            return "WebDAV 未配置地址（先用 /webdav 配置）"
        deleted = 0
        failed_names = []
        for f in log.get("files", []):
            if f.get("status") == "deleted":
                deleted += 1
                continue
            ok = await asyncio.to_thread(
                webdav.delete_remote,
                cfg.get("url"),
                log["remote_dir"],
                f["name"],
                cfg.get("user"),
                cfg.get("pass"),
            )
            if ok:
                deleted += 1
                f["status"] = "deleted"
            else:
                failed_names.append(f["name"])
        if deleted:
            self.webdav_keep_cache.discard(log["seq"])
            self._schedule_cleanup(log["seq"], "")
        if failed_names:
            self._save_webdav_logs()
            return f"❌ 删除失败 {len(failed_names)} 个: {', '.join(failed_names[:3])}"
        self.webdav_logs.pop(key, None)
        self._save_webdav_logs()
        return f"🗑 已删除本次上传的 {deleted} 个文件"

    async def _webdav_autoretry_loop(self) -> None:
        """失败记录自动重传循环：每 WEBDAV_AUTORETRY_INTERVAL（默认 1 小时）扫描一次，
        对状态非 ok 且本地缓存仍在的文件重传到原 remote_dir，直到全部完成。"""
        while True:
            if self._stopping:
                return
            try:
                await self._webdav_autoretry_once()
            except Exception as exc:
                logger.error("WebDAV 自动重传异常: %s", exc)
            await asyncio.sleep(WEBDAV_AUTORETRY_INTERVAL)

    async def _webdav_autoretry_once(self) -> None:
        cfg = self.webdav_cfg
        if not cfg.get("enabled") or not cfg.get("url"):
            return
        if getattr(self, "backup_manager", None) is not None and getattr(self, "repository", None) is not None:
            retried = await self.backup_manager.autoretry_durable_once()
            if retried:
                logger.info("WebDAV durable 自动重传完成：本次成功 %d 个文件", retried)
            return
        if not self.webdav_logs:
            return
        retried = 0
        for key, log in list(self.webdav_logs.items()):
            files = log.get("files", [])
            pending = [f for f in files if f.get("status") not in ("ok", "deleted")]
            if not pending:
                continue
            if getattr(self, "backup_manager", None) is not None and getattr(self, "repository", None) is not None:
                due, retry_at = await self.backup_manager.retry_due(
                    int(log.get("seq") or 0), str(log.get("remote_dir") or "")
                )
                if not due:
                    logger.info(
                        "WebDAV 自动重传等待 durable retry window job=%s retry_at=%s",
                        log.get("seq"), retry_at,
                    )
                    continue
            changed = False
            all_ok = True
            for f in pending:
                local = f.get("local", "")
                try:
                    ok = await self._webdav_transfer_file(
                        seq=log["seq"], remote_dir=log["remote_dir"],
                        file=f, cfg=cfg,
                    )
                except Exception as exc:
                    logger.warning("自动重传 %s 异常: %s", f.get("name"), exc)
                    ok = False
                if ok:
                    f["status"] = "ok"
                    retried += 1
                    logger.info(
                        "WebDAV 自动重传成功 %s -> %s/%s",
                        f.get("name"),
                        log["remote_dir"],
                        f.get("name"),
                    )
                else:
                    all_ok = False
                    logger.info(
                        "WebDAV 自动重传失败（稍后重试）%s -> %s",
                        f.get("name"),
                        log["remote_dir"],
                    )
                changed = True
            if changed:
                await self._webdav_finalize_attempt(log["seq"], log["remote_dir"], files)
                if all_ok:
                    self.webdav_keep_cache.discard(log["seq"])
                    self._schedule_cleanup(log["seq"], "")
                    await self._notify_webdav_autoretry_done(log)
                self._save_webdav_logs()
        if retried:
            logger.info("WebDAV 自动重传完成：本次成功 %d 个文件", retried)

    async def _wait_webdav(self, seq: int, timeout: float | None = None) -> None:
        """等待某任务的 WebDAV 后台上传结束（清理缓存前调用，防删除未传完文件）。"""
        pending = [t for t, s in self._webdav_tasks.items() if s == seq]
        if not pending:
            return
        done, remaining = await asyncio.wait(pending, timeout=timeout)
        for t in pending:
            if t in done and not t.cancelled():
                try:
                    t.result()
                except Exception:
                    pass
        for t in remaining:
            t.cancel()
        for t in remaining:
            try:
                await t
            except Exception:
                pass

    async def _on_upload_progress(
        self, seq: int, received: int, total: int, item: int, items: int
    ) -> None:
        job = self.jobs.get(seq)
        if job is None:
            return
        if self._progress_tracker.phase_is_stale(seq, "publishing"):
            return
        owner = self._repo_claim_owners.get(("publish", seq))
        if owner and getattr(self, "job_queue", None) is not None:
            await self.job_queue.heartbeat_claim(seq, "publish", owner)
        progress = self._progress_tracker.update(
            seq, "publishing", received, total, item, items
        )
        overall = progress.pct
        self.active[seq] = {
            "phase": "upload",
            "pct": overall,
            "item": item,
            "items": items,
            "user_id": job.user_id,
        }
        if (
            getattr(self, "repository", None) is not None
            and getattr(self, "job_queue", None) is not None
            and self._progress_tracker.allow_db(progress)
        ):
            await self.job_queue.persist_progress(
                seq,
                received=received,
                total=total,
                item=item,
                items=items,
            )
        await self._update_progress_status(seq)

    async def _on_pre_publish(self, job, payload) -> None:
        profiles = getattr(self, "destination_profiles", None)
        snapshot = None
        if getattr(self, "repository", None) is not None and getattr(self, "job_queue", None) is not None:
            job_id = self.job_queue.durable_job_id(job.seq)
            if job_id is not None:
                record = await self.repository.get_job(job_id)
                if record is not None and record.destination_profile_snapshot_json:
                    try:
                        snapshot = json.loads(record.destination_profile_snapshot_json)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        snapshot = None
        if snapshot is None:
            runtime_snapshot = getattr(job, "destination_profile_snapshot", None)
            if isinstance(runtime_snapshot, dict):
                snapshot = dict(runtime_snapshot)
        if snapshot is None and profiles is not None:
            snapshot = profiles.current_snapshot()
        if snapshot:
            footer = profiles.render_snapshot_footer(snapshot) if profiles is not None else None
            self.publisher.configure_destination_profile(snapshot, footer=footer)
            job._destination_profile_snapshot = dict(snapshot)
            job.destination_profile_id = snapshot.get("profile_id")
            job.destination_profile_snapshot = dict(snapshot)
            job._destination_key = str(snapshot.get("destination_peer") or DEST_CHANNEL)
            job._backup_policy = str(snapshot.get("backup_policy") or "best_effort")
        else:
            job._destination_key = str(DEST_CHANNEL)
            job._backup_policy = str(self.webdav_cfg.get("backup_policy") or "best_effort")
        self._progress_tracker.begin_phase(job.seq, "publishing")
        if getattr(self, "job_queue", None) is not None and job.seq not in self._repo_publish_claimed:
            self.job_queue.shadow_transition(job.seq, "publish_started", "publishing")
        text, buttons = self._job_card(job, "publishing", payload=payload)
        await self._safe_edit(job, text, buttons=buttons)

    async def _on_publish_checkpoint(
        self, job, refs: list[tuple[int, int, str]]
    ) -> None:
        if getattr(self, "repository", None) is not None and getattr(self, "job_queue", None) is not None:
            await self.job_queue.checkpoint_publish(job.seq, refs)

    async def _on_published(self, job, ids: list) -> None:
        self._remember_published(job.seq, ids)
        if (
            bool(self.webdav_cfg.get("enabled"))
            and str(getattr(job, "_backup_policy", self.webdav_cfg.get("backup_policy") or "best_effort")) == "required"
            and getattr(self, "repository", None) is not None
        ):
            job._required_backup_pending = True
            try:
                await self._safe_edit(
                    job,
                    f"☁️ 任务 #{job.seq} · Telegram 已发布\n──────────\n正在等待 required WebDAV 备份完成…",
                )
            except Exception:
                pass
            return
        self._progress_tracker.begin_phase(job.seq, "succeeded")
        if getattr(self, "job_queue", None) is not None:
            if getattr(self, "repository", None) is not None:
                durable_refs = list(getattr(job, "_published_refs", []) or [])
                await self.job_queue.complete_publish(job.seq, durable_refs or ids)
            else:
                self.job_queue.shadow_published(job.seq, ids)
        text, buttons = self._job_card(job, "succeeded")
        await self._safe_edit(job, text, buttons=buttons)
        if AUTO_DELETE_SECONDS > 0:
            asyncio.get_running_loop().create_task(
                _delete_after(job.status, AUTO_DELETE_SECONDS)
            )

    async def _finish_required_backup(self, job) -> bool:
        """Finalize a published job only after required WebDAV backup settles."""
        seq = int(job.seq)
        await self._wait_webdav(seq)
        outcome = None
        if getattr(self, "backup_manager", None) is not None:
            outcome = await self.backup_manager.required_outcome(seq)
        if outcome is not None and str(outcome.get("state")) == "succeeded":
            self._progress_tracker.begin_phase(seq, "succeeded")
            if getattr(self, "job_queue", None) is not None:
                durable_refs = list(getattr(job, "_published_refs", []) or [])
                await self.job_queue.complete_publish(seq, durable_refs or self.published.get(seq, []))
            text, buttons = self._job_card(job, "succeeded")
            await self._safe_edit(job, text, buttons=buttons)
            if AUTO_DELETE_SECONDS > 0:
                asyncio.get_running_loop().create_task(
                    _delete_after(job.status, AUTO_DELETE_SECONDS)
                )
            return True

        code = str((outcome or {}).get("error_code") or "webdav_server")
        summary = str((outcome or {}).get("error_message") or "required WebDAV 备份未成功")
        self._progress_tracker.begin_phase(seq, "failed")
        if getattr(self, "job_queue", None) is not None:
            await self.job_queue.transition_now(
                seq,
                "required_backup_failed",
                "failed",
                error_code=code,
                error_message=summary,
                phase="backup",
            )
        text, buttons = self._job_card(
            job,
            "failed",
            error=f"WebDAV required 备份失败：{summary}",
        )
        await self._safe_edit(job, text, buttons=buttons)
        return False

    async def _update_progress_status(self, seq: int) -> None:
        job = self.jobs.get(seq)
        info = self.active.get(seq)
        if job is None or info is None:
            return
        phase = "downloading" if info["phase"] == "download" else "publishing"
        state = self._progress_tracker.state(seq, phase)
        if state is None or not self._progress_tracker.allow_ui(state):
            return
        text, buttons = self._job_card(job, phase, progress=state)
        await self._safe_edit(job, text, buttons=buttons)

    def _job_card(self, job, phase: str, *, payload=None, progress=None, error: str | None = None):
        media_count = len(job.album or []) if getattr(job, "album", None) else 1
        total_bytes = None
        if payload:
            paths = payload if isinstance(payload, list) else [payload]
            try:
                total_bytes = sum(
                    os.path.getsize(path)
                    for path in paths
                    if path and os.path.isfile(path)
                )
            except OSError:
                total_bytes = None
        view = JobCardView(
            seq=job.seq,
            phase=phase,
            media_count=max(media_count, getattr(progress, "items", 1) if progress else 1),
            total_bytes=total_bytes or (getattr(progress, "total", None) if progress else None),
            transferred_bytes=(getattr(progress, "received", None) if progress else None),
            pct=getattr(progress, "pct", None),
            speed_bps=getattr(progress, "speed_bps", None),
            eta_seconds=getattr(progress, "eta_seconds", None),
            item=getattr(progress, "item", 1),
            items=getattr(progress, "items", media_count),
            show_progress=self._show_progress(job.user_id),
            error=error,
            cache_retained=bool(getattr(job, "cached_path", "")),
            compat_note=" · ".join(getattr(job, "_media_compat_notes", [])[:2]),
        )
        return render_job_card_view(view)

    async def _safe_edit(self, job, text: str, buttons=None) -> None:
        if job is None:
            return
        try:
            await job.status.edit(text, buttons=buttons)
        except Exception as exc:
            if isinstance(exc, FloodWaitError):
                self._progress_tracker.defer_ui(getattr(exc, "seconds", 1))
                logger.warning("Task card edit rate-limited for %ss", getattr(exc, "seconds", 1))
                return
            if (
                getattr(self, "repository", None) is None
                or not getattr(job, "user_id", None)
                or job.seq in self._status_rebound
            ):
                return
            self._status_rebound.add(job.seq)
            try:
                status = await self.client.send_message(job.user_id, text, buttons=buttons)
            except Exception:
                return
            job.status = status
            if getattr(self, "job_queue", None) is not None:
                await self.job_queue.set_status_reference(job.seq, status, chat_id=job.user_id)

    async def _delete_status(self, job) -> None:
        if job is None:
            return
        try:
            await job.status.delete()
        except Exception:
            pass

    async def _upload_worker(self) -> None:
        watchdog = DOWNLOAD_TIMEOUT + 60
        while True:
            if self._stopping:
                return
            while self._paused:
                await asyncio.sleep(1)
            # 下载优先：有未下载任务（排队中或下载中）→ 挂起上传
            while not self.input_q.empty() or self._active_downloads > 0:
                await asyncio.sleep(0.5)
            seq = None
            if getattr(self, "repository", None) is not None and getattr(self, "job_queue", None) is not None:
                await self.job_queue.drain_shadow()
                claim = await self.job_queue.claim_next_publish("publish:primary")
                if claim is not None:
                    seq = int(claim.job.legacy_seq if claim.job.legacy_seq is not None else claim.job.id)
                    self._repo_publish_claimed.add(seq)
                    self._repo_claim_owners[("publish", seq)] = "publish:primary"
            else:
                seq = self._pick_next_upload()
            if seq is None:
                await self._watchdog_unresolved(watchdog)
                await asyncio.sleep(1)
                continue
            if getattr(self, "repository", None) is not None:
                path = await self.job_queue.durable_payload(seq)
                if path is None:
                    logger.error("Claimed publish job #%s has no durable local payload", seq)
                    await self.job_queue.transition_now(
                        seq,
                        "publish_payload_missing",
                        "failed",
                        reason="durable_local_path_missing",
                    )
                    self._finish_seq(seq, keep_cache=True)
                    self._repo_publish_claimed.discard(seq)
                    self._repo_claim_owners.pop(("publish", seq), None)
                    continue
            else:
                fut = self.results[seq]
                try:
                    path = fut.result()
                except asyncio.CancelledError:
                    continue
                except Exception as exc:
                    await self._reply_error(
                        seq, f"下载失败: {exc}", retry_job=self.jobs.get(seq)
                    )
                    self._finish_seq(seq)
                    continue
            if path is _CANCELLED:
                logger.info("Job #%s skipped (cancelled)", seq)
                self._finish_seq(seq)
                continue
            job = self.jobs.get(seq)
            if seq in self._cancel_marked:
                logger.info("Job #%s cancelled before upload", seq)
                self._cancel_marked.discard(seq)
                await self._delete_status(job)
                self._finish_seq(seq)
                continue
            if seq in self._paused_files:
                continue

            self._uploading = seq
            publish_retries = 0
            try:
                while True:
                    if seq in self._cancel_marked:
                        self._cancel_marked.discard(seq)
                        await self._delete_status(job)
                        self._finish_seq(seq)
                        break
                    task = asyncio.get_running_loop().create_task(
                        self.publisher.publish(job, path)
                    )
                    self._upload_tasks[seq] = task
                    try:
                        done, _ = await asyncio.wait({task}, timeout=UPLOAD_TIMEOUT)
                    except asyncio.CancelledError:
                        logger.info("Job #%s upload worker cancelled", seq)
                        if not self._stopping:
                            self._cancel_marked.discard(seq)
                            await self._delete_status(job)
                            self._finish_seq(seq)
                        break

                    if task in done:
                        try:
                            await task
                        except asyncio.CancelledError:
                            if self._stopping:
                                logger.info("Job #%s upload interrupted by shutdown", seq)
                            else:
                                logger.info("Job #%s upload stopped by user", seq)
                                self._cancel_marked.discard(seq)
                                await self._delete_status(job)
                                self._finish_seq(seq)
                            break
                        except FileTooLargeError as exc:
                            logger.warning("Job #%s %s", seq, exc)
                            await self._reply_error(
                                seq,
                                "上传失败：文件超过当前发布上限",
                                exc=exc,
                                phase="publish",
                            )
                            self._finish_seq(seq)
                            break
                        except Exception as exc:
                            has_side_effect_risk = (
                                int(getattr(job, "_publish_send_attempts", 0)) > 0
                                or bool(getattr(job, "_published_refs", []))
                            )
                            effective_exc = exc
                            if has_side_effect_risk and not isinstance(exc, PublishPartialError):
                                effective_exc = PublishPartialError()
                            error = classify_error(effective_exc, stage="publish")
                            decision = self._retry_policy.decide(
                                error,
                                stage="publish",
                                attempt=publish_retries + 1,
                                now=time.time(),
                            )
                            if not has_side_effect_risk and decision.should_retry:
                                publish_retries = decision.attempt
                                if getattr(self, "repository", None) is not None:
                                    await self.job_queue.record_retry(
                                        seq,
                                        phase="publish",
                                        error_code=error.code.value,
                                        error_message=error.summary,
                                        retry_count=publish_retries,
                                        next_retry_at=float(decision.next_retry_at),
                                    )
                                logger.warning(
                                    "Job #%s publish failed code=%s, auto-retry %d/%d in %.1fs",
                                    seq,
                                    error.code.value,
                                    publish_retries,
                                    decision.budget,
                                    float(decision.delay_seconds),
                                )
                                if await self._wait_retry(seq, float(decision.delay_seconds)):
                                    self._cancel_marked.discard(seq)
                                    await self._delete_status(job)
                                    self._finish_seq(seq)
                                    break
                                continue
                            logger.error(
                                "Publish failed job=%s phase=publish code=%s exception_type=%s traceback=%s",
                                seq,
                                error.code.value,
                                effective_exc.__class__.__name__,
                                safe_traceback(exc),
                            )
                            await self._reply_error(
                                seq,
                                f"上传失败: {exc}",
                                retry_job=job,
                                retry_path=path if not isinstance(path, list) else "",
                                exc=effective_exc,
                                phase="publish",
                                retry_count=publish_retries,
                            )
                            self._finish_seq(seq, keep_cache=True)
                            break
                        else:
                            if bool(getattr(job, "_required_backup_pending", False)):
                                backup_ok = await self._finish_required_backup(job)
                                self._finish_seq(seq, keep_cache=not backup_ok)
                            else:
                                self._finish_seq(seq)
                            break

                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    has_side_effect_risk = (
                        int(getattr(job, "_publish_send_attempts", 0)) > 0
                        or bool(getattr(job, "_published_refs", []))
                    )
                    effective_exc = PublishPartialError() if has_side_effect_risk else TimeoutError("publish timeout")
                    error = classify_error(effective_exc, stage="publish")
                    decision = self._retry_policy.decide(
                        error,
                        stage="publish",
                        attempt=publish_retries + 1,
                        now=time.time(),
                    )
                    if not has_side_effect_risk and decision.should_retry:
                        publish_retries = decision.attempt
                        if getattr(self, "repository", None) is not None:
                            await self.job_queue.record_retry(
                                seq,
                                phase="publish",
                                error_code=error.code.value,
                                error_message=error.summary,
                                retry_count=publish_retries,
                                next_retry_at=float(decision.next_retry_at),
                            )
                        logger.warning(
                            "Job #%s publish timeout, auto-retry %d/%d in %.1fs",
                            seq,
                            publish_retries,
                            decision.budget,
                            float(decision.delay_seconds),
                        )
                        if await self._wait_retry(seq, float(decision.delay_seconds)):
                            self._cancel_marked.discard(seq)
                            await self._delete_status(job)
                            self._finish_seq(seq)
                            break
                        continue
                    logger.error(
                        "Upload for job #%s timed out after %ss code=%s",
                        seq,
                        UPLOAD_TIMEOUT,
                        error.code.value,
                    )
                    await self._reply_error(
                        seq,
                        f"上传超时（{UPLOAD_TIMEOUT} 秒）",
                        retry_job=job,
                        retry_path=path if not isinstance(path, list) else "",
                        exc=effective_exc,
                        phase="publish",
                        retry_count=publish_retries,
                    )
                    self._finish_seq(seq, keep_cache=True)
                    break
            finally:
                self._upload_tasks.pop(seq, None)
                self._repo_publish_claimed.discard(seq)
                self._repo_claim_owners.pop(("publish", seq), None)
                self._uploading = None

    def _pick_next_upload(self):
        for seq in sorted(self.results):
            if seq in self._paused_files:
                continue
            if self.results[seq].done():
                return seq
        return None

    async def _watchdog_unresolved(self, watchdog: int) -> None:
        now = time.time()
        for seq, fut in list(self.results.items()):
            if fut.done():
                continue
            created = self._future_created.get(seq, now)
            if now - created >= watchdog:
                logger.error(
                    "Job #%s unresolved for %ss, forcing cancel", seq, watchdog
                )
                await self._reply_error(
                    seq,
                    f"处理超时（{watchdog} 秒）",
                    retry_job=self.jobs.get(seq),
                )
                self._set_cancelled(seq)
                self._future_created[seq] = now

    def _finish_seq(self, seq: int, keep_cache: bool = False) -> None:
        job = self.jobs.pop(seq, None)
        cleanup = getattr(job, "cleanup_extra", "") if job else ""
        self.results.pop(seq, None)
        self.active.pop(seq, None)
        self.active_seqs.discard(seq)
        self._cancel_marked.discard(seq)
        self._paused_files.discard(seq)
        self._future_created.pop(seq, None)
        self._runtime_jobs.pop(seq, None)
        if getattr(self, "disk", None) is not None:
            self.disk.release(seq)
        if not keep_cache:
            self._schedule_cleanup(seq, cleanup)

    def _schedule_cleanup(self, seq: int, cleanup: str) -> None:
        """清理缓存目录；若该任务仍有 WebDAV 后台上传在跑，则等上传结束再删。

        WebDAV 上传存在失败文件（webdav_keep_cache）时保留缓存，供 /webdav 记录内重试。
        """
        if seq in self.webdav_keep_cache:
            logger.info("Job #%s webdav 有失败文件，保留缓存待 /webdav 记录内重试", seq)
            return

        def _do_cleanup() -> None:
            shutil.rmtree(self._workdir(seq), ignore_errors=True)
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)

        pending = [t for t, s in self._webdav_tasks.items() if s == seq]
        if not pending:
            _do_cleanup()
            return

        async def _delayed() -> None:
            await self._wait_webdav(seq)
            if seq in self.webdav_keep_cache:
                logger.info(
                    "Job #%s webdav 上传失败，保留缓存待自动/手动重传", seq
                )
                return
            _do_cleanup()

        try:
            asyncio.get_running_loop().create_task(_delayed())
        except RuntimeError:
            if seq not in self.webdav_keep_cache:
                _do_cleanup()

    def _remember_published(self, seq: int, ids: list) -> None:
        self.published[seq] = ids
        if len(self.published) > 50:
            for old_seq in sorted(self.published)[:-50]:
                self.published.pop(old_seq, None)

    def _workdir(self, seq: int) -> str:
        return os.path.join(DOWNLOAD_DIR, f"job-{seq}")

    def _set_result(self, seq: int, value: str) -> None:
        fut = self.results.get(seq)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self.results[seq] = fut
            self._future_created[seq] = time.time()
        if not fut.done():
            fut.set_result(value)

    def _set_exception(self, seq: int, exc: Exception) -> None:
        fut = self.results.get(seq)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self.results[seq] = fut
            self._future_created[seq] = time.time()
        if not fut.done():
            fut.set_exception(exc)

    def _set_cancelled(self, seq: int) -> None:
        fut = self.results.get(seq)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self.results[seq] = fut
            self._future_created[seq] = time.time()
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
        retry_interrupt = self._retry_interrupts.get(seq)
        if retry_interrupt is not None:
            retry_interrupt.set()
        self._set_cancelled(seq)
        job = self.jobs.get(seq) or self._runtime_jobs.get(seq)
        token = getattr(job, "url_cancel_token", None) if job is not None else None
        if token is not None:
            token.cancel()
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
        texts: list = None,
        reserved_seq: int | None = None,
        spoiler_override: bool | None = None,
        destination_profile_id: int | None = None,
        destination_profile_snapshot: dict | None = None,
        allow_album_merge: bool = True,
    ) -> int:
        # 队列级合并：同一用户已有"入队未下载"的相册任务时，追加消息而非新建任务
        if allow_album_merge and reserved_seq is None and kind == "album" and album:
            existing_seq = self.pending_albums.get(user_id)
            if existing_seq is not None:
                existing = self.album_jobs.get(existing_seq)
                if existing is not None and not existing.started:
                    seen = {m.id for m in (existing.album or [])}
                    added = [m for m in album if getattr(m, "id", None) not in seen]
                    if added:
                        existing.album.extend(added)
                        self._reserve_disk_for_job(existing)
                        if getattr(self, "job_queue", None) is not None:
                            self.job_queue.shadow_accept_legacy(
                                existing_seq,
                                kind=existing.kind,
                                user_id=user_id,
                                state="queued",
                                message=existing.message,
                                album=added,
                                texts=existing.texts,
                                spoiler=existing.spoiler,
                                status=existing.status,
                                status_chat_id=user_id,
                            )
                        try:
                            await existing.status.edit(
                                f"🔄 相册已合并，共 {len(existing.album)} 条，等待处理"
                            )
                        except Exception:
                            pass
                        logger.info(
                            "Album merged into job #%s (+%d 条, 共 %d)",
                            existing_seq,
                            len(added),
                            len(existing.album),
                        )
                    return existing_seq

        mode = self._spoiler_mode(user_id)
        if spoiler_override is not None:
            spoiler = bool(spoiler_override)
            label = "🔞 雪花遮挡" if spoiler else "✅ 正常"
        elif force_normal:
            spoiler = False
            label = "✅ 正常"
        else:
            spoiler = mode == "always_spoiler"
            label = "🔞 雪花遮挡" if spoiler else "✅ 正常"
        seq = reserved_seq if reserved_seq is not None else self.reserve_seq()
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
        job = _Job(
            seq=seq,
            kind=kind,
            status=status,
            message=message,
            album=album,
            spoiler=spoiler,
            user_id=user_id,
            texts=texts,
            destination_profile_id=(
                destination_profile_id
                if destination_profile_id is not None
                else (
                    self.destination_profiles.current_profile.id
                    if getattr(self, "destination_profiles", None) is not None
                    else None
                )
            ),
            destination_profile_snapshot=(
                dict(destination_profile_snapshot)
                if destination_profile_snapshot is not None
                else (
                    self.destination_profiles.current_snapshot()
                    if getattr(self, "destination_profiles", None) is not None
                    else None
                )
            ),
        )
        if kind == "album":
            self.album_jobs[seq] = job
            self.pending_albums[user_id] = seq
        self.enqueue(job)
        if getattr(self, "job_queue", None) is not None:
            self.job_queue.shadow_accept_legacy(
                seq,
                kind=kind,
                user_id=user_id,
                state="queued",
                message=message,
                album=album,
                texts=texts,
                spoiler=spoiler,
                status=status,
                status_chat_id=user_id,
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
        texts: list = None,
    ) -> None:
        self.register_pending(
            seq, kind, message, album=album, user_id=user_id, texts=texts
        )
        if kind == "collection":
            text = f"⚠️ 该合集（{len(album)} 个媒体）是否为 18+？"
        elif kind == "album":
            text = f"⚠️ 该相册（{len(album)} 张）是否为 18+？"
        else:
            text = "⚠️ 该内容是否为 18+？"
        try:
            pending = self.pending.get(seq)
            profile_name = (
                pending.destination_profile_name
                if pending is not None and pending.destination_profile_name
                else "默认频道"
            )
            status = await self.client.send_message(
                chat_id,
                text,
                buttons=[
                    [
                        Button.inline("🔞 是（雪花遮挡）", f"confirm:{seq}:1"),
                        Button.inline("✅ 否", f"confirm:{seq}:0"),
                    ],
                    [Button.inline(f"🎯 {profile_name[:30]}", f"cp:{seq}")],
                    [Button.inline("❌ 取消", f"cancel:{seq}")],
                ],
            )
            self.set_pending_status(seq, status)
            if getattr(self, "job_queue", None) is not None:
                self.job_queue.shadow_accept_legacy(
                    seq,
                    kind=kind,
                    user_id=user_id,
                    state="awaiting_confirmation",
                    message=message,
                    album=album,
                    texts=texts,
                    status=status,
                    status_chat_id=chat_id,
                )
        except Exception:
            self.pending.pop(seq, None)
            self._set_cancelled(seq)
            raise

    async def _reply_error(
        self,
        seq: int,
        text: str,
        retry_job: _Job = None,
        retry_path: str = "",
        *,
        exc: BaseException | None = None,
        phase: str = "unknown",
        retry_count: int = 0,
        next_retry_at: float | None = None,
    ) -> None:
        job = self.jobs.get(seq)
        self._progress_tracker.begin_phase(seq, "failed")
        error = classify_error(exc or RuntimeError(text), stage=phase)
        display_text = text
        if exc is not None:
            prefix = text.split(":", 1)[0].split("：", 1)[0]
            display_text = f"{prefix}：{error.summary}"
        if retry_job is not None and error.retryable:
            self.retryable[seq] = _RetryInfo(job=retry_job, path=retry_path)
        if getattr(self, "job_queue", None) is not None:
            if getattr(self, "repository", None) is not None:
                await self.job_queue.transition_now(
                    seq,
                    "failed",
                    "failed",
                    error_code=error.code.value,
                    error_message=error.summary if exc is not None else display_text,
                    retry_count=retry_count,
                    next_retry_at=next_retry_at,
                    phase=phase,
                )
            else:
                self.job_queue.shadow_transition(seq, "failed", "failed")
        if job is not None:
            card, buttons = self._job_card(
                job,
                "failed",
                payload=retry_path or getattr(job, "cached_path", ""),
                error=display_text,
            )
            await self._safe_edit(job, card, buttons=buttons)


def register_handlers(
    client: TelegramClient,
    repository=None,
    *,
    default_destination_profile=None,
    start_workers: bool = True,
    settings: Settings | None = None,
):
    """Build the pipeline and install the extracted R1 handler layer."""
    static = settings or _legacy_static_settings()
    pipeline = _Pipeline(client, settings=static)
    # R2-A lifecycle seam only: the in-memory pipeline remains the runtime
    # source of truth until the explicit R2-B dual-write migration stage.
    pipeline.repository = repository
    shadow = ShadowState(repository)
    queue = JobQueue(pipeline, shadow=shadow)
    backup = BackupManager(pipeline, shadow=shadow)
    proxy = ProxyManager(pipeline)
    interactions = InteractionSessions()
    operations = OperationStore()
    stats = StatsService(pipeline, repository, backup) if repository is not None else None
    pipeline.job_queue = queue
    pipeline.backup_manager = backup
    pipeline.proxy_manager = proxy
    pipeline.interactions = interactions
    pipeline.operations = operations
    pipeline.stats_service = stats
    pipeline.destination_profiles = (
        DestinationProfileManager(repository, default_destination_profile)
        if repository is not None and default_destination_profile is not None
        else None
    )
    pipeline.source_profiles = SourceProfileManager(repository) if repository is not None else None
    pipeline.dedup_manager = DedupManager(repository, destination_key=str(static.dest_channel)) if repository is not None else None
    pipeline.publisher.dedup_manager = pipeline.dedup_manager
    pipeline.shadow_state = shadow
    ctx = HandlerContext(
        client=client,
        pipeline=pipeline,
        queue=queue,
        backup=backup,
        proxy=proxy,
        interactions=interactions,
        operations=operations,
        stats=stats,
        allowed_users=set(static.allowed_users),
        auto_delete_seconds=static.auto_delete_seconds,
        session_collect=static.session_collect,
        media_types=MEDIA_TYPES,
        url_re=URL_RE,
        dest_channel=static.dest_channel,
        start_text=_START_TEXT,
        about_text=_ABOUT_TEXT,
        delete_after=_delete_after,
        destinations=pipeline.destination_profiles,
        sources=pipeline.source_profiles,
    )
    install_handlers(ctx)
    if start_workers:
        pipeline.start()
    return pipeline
