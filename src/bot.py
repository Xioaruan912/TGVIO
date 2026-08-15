import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from telethon import Button, TelegramClient, events
from telethon.tl.types import (
    KeyboardButton,
    KeyboardButtonRow,
    MessageEntityBotCommand,
    MessageMediaDocument,
    MessageMediaPhoto,
    ReplyKeyboardMarkup,
)

from .config import (
    ALLOWED_USERS,
    AUTO_DELETE_SECONDS,
    CHANNEL_AT,
    COLLECTION_GATHER_SECONDS,
    CONFIRM_TIMEOUT,
    COVER_MODE,
    COVER_WIDTH,
    DEST_CHANNEL,
    DOWNLOAD_AUTO_RETRY,
    DOWNLOAD_CONCURRENCY,
    DOWNLOAD_DIR,
    DOWNLOAD_TIMEOUT,
    DOWNLOAD_WORKERS,
    FORWARD_CAPTION,
    GROUP_AT,
    MAX_COVER_IMAGES,
    MAX_FILE_SIZE,
    PART_SIZE_KB,
    PROGRESS_MIN_INTERVAL,
    SESSION_COLLECT,
    SESSION_END_TIMEOUT,
    UPLOAD_TIMEOUT,
    UPLOAD_WORKERS,
    WEBDAV_ENABLED,
    WEBDAV_PASS,
    WEBDAV_PATH,
    WEBDAV_RETRY,
    WEBDAV_URL,
    WEBDAV_USER,
)
from . import webdav
from .media import FileTooLargeError, MediaDownloader, MediaPublisher

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
PROXY_FILE = os.path.join("session", "proxy.json")

_NETWORK_ERROR_NAMES = {
    "TimedOutError",
    "ServerError",
    "RpcCallFailError",
    "RpcMcgetFailError",
    "InterdcCallErrorError",
    "InterdcCallRichErrorError",
    "NetworkError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "ConnectionRefusedError",
    "TimeoutError",
}


def _is_network_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        return True
    name = exc.__class__.__name__
    if name in _NETWORK_ERROR_NAMES:
        return True
    if "Request was unsuccessful" in str(exc):
        return True
    return False

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


_SESSION_BTN_BEGIN = "📥 开始合集"
_SESSION_BTN_END = "🛑 结束合集"


def _reply_keyboard() -> ReplyKeyboardMarkup:
    """打字框上方的常驻回复键盘（点击按钮即发送对应文本，走命令处理逻辑）。"""
    return ReplyKeyboardMarkup(
        rows=[
            KeyboardButtonRow(
                buttons=[
                    KeyboardButton(_SESSION_BTN_BEGIN),
                    KeyboardButton(_SESSION_BTN_END),
                ]
            )
        ],
        resize=True,
        persistent=True,
        single_use=False,
        selective=False,
    )


def render_bar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, round(pct * width / 100)))
    return "█" * filled + "░" * (width - filled)


def _pos_token(n: int) -> str:
    if 1 <= n <= 9:
        return "①②③④⑤⑥⑦⑧⑨"[n - 1]
    return str(n)


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
    "1. 转发含视频/图片的消息给我 → 自动开始合集会话，继续转发自动并入 → 点「🛑 结束并发布」或发 /end 发布到频道\n"
    "2. 发送一个链接（抖音/B站/YouTube 等）→ 自动下载并发布到频道\n\n"
    "合集：图片进频道封面相册（超过 10 张按序丢弃），全部视频整合进同一个评论区；\n"
    "会话期间发的文字消息会作为评论，结束时整合为封面文字与封面一起发送。\n\n"
    "18+ 处理默认「总是正常」；需要雪花遮挡请用 /mode 设置「总是雪花遮挡」或「每次询问」。\n"
    "选「是（雪花遮挡）」时，用 Telegram 内置雪花效果遮挡发布，文件内容不被修改。\n\n"
    f"⚠️ 确认弹窗 {CONFIRM_TIMEOUT} 秒内未回复将自动取消该任务。\n"
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
    "/webdav   配置 WebDAV 备份 / 查看上传记录（📁 按钮进入，可重试/删除）\n"
    "/proxy    代理设置（HTTP 代理，下载失败自动切换）\n"
    "/queue    管理队列（逐项取消/暂停/恢复）\n"
    "/begin    开始合集会话（转发会自动开始）\n"
    "/end      结束合集并发布（所有视频进同一个评论区）"
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
    cached_path: str = ""
    cleanup_extra: str = ""
    started: bool = False
    texts: list = None


@dataclass
class _RetryInfo:
    job: object
    path: str = ""


@dataclass
class _PendingJob:
    seq: int
    kind: str
    message: object = None
    album: list = None
    status: object = None
    timeout_task: object = None
    user_id: int = 0
    texts: list = None


@dataclass
class _AlbumBuffer:
    chat_id: int
    messages: list
    grouped_ids: set
    task: object = None


@dataclass
class _Session:
    user_id: int
    items: list = None
    status: object = None
    button_task: object = None
    started_at: float = 0.0
    texts: list = None

    def __post_init__(self) -> None:
        if self.items is None:
            self.items = []
        if self.texts is None:
            self.texts = []
        if not self.started_at:
            self.started_at = time.time()

    @property
    def media_count(self) -> int:
        return sum(len(item) for item in self.items)

    @property
    def text_count(self) -> int:
        return len(self.texts)


class _Pipeline:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.input_q: asyncio.Queue = asyncio.Queue()
        self.jobs: dict[int, _Job] = {}
        self.pending: dict[int, _PendingJob] = {}
        self.albums: dict[int, _AlbumBuffer] = {}
        self.album_jobs: dict[int, _Job] = {}
        self.pending_albums: dict[int, int] = {}
        self.results: dict[int, asyncio.Future] = {}
        self.active: dict[int, dict] = {}
        self.active_seqs: set[int] = set()
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
        self._paused_files: set[int] = set()
        self._future_created: dict[int, float] = {}
        self.sessions: dict[int, _Session] = {}
        self._webdav_tasks: dict[asyncio.Task, int] = {}
        self.webdav_cfg = self._load_webdav_cfg()
        self.webdav_logs: dict = self._load_webdav_logs()
        self.webdav_keep_cache: set[int] = set()
        self.webdav_waiting: dict[int, str] = {}
        self.webdav_count: dict = self._load_webdav_count()
        self._webdav_count_lock = asyncio.Lock()
        self.proxy_cfg: dict = self._load_proxy_cfg()
        self.proxy_waiting: dict[int, str] = {}
        self._load_prefs()

        self.downloader = MediaDownloader(
            client, self._workdir, DOWNLOAD_TIMEOUT, DOWNLOAD_WORKERS, PART_SIZE_KB
        )
        self.publisher = MediaPublisher(
            client,
            DEST_CHANNEL,
            self._workdir,
            UPLOAD_TIMEOUT,
            MAX_FILE_SIZE,
            FORWARD_CAPTION,
            UPLOAD_WORKERS,
            PART_SIZE_KB,
            cover_mode=COVER_MODE,
            cover_width=COVER_WIDTH,
            max_cover_images=MAX_COVER_IMAGES,
            group_counter_file=os.path.join("session", "group_counter.txt"),
            channel_at=CHANNEL_AT,
            group_at=GROUP_AT,
        )
        self.downloader.pre_download_hooks.append(self._on_pre_download)
        self.downloader.progress_hooks.append(self._on_download_progress)
        self.downloader.post_download_hooks.append(self._on_download_done)
        self.downloader.post_download_hooks.append(self._on_webdav_upload)
        self.publisher.progress_hooks.append(self._on_upload_progress)
        self.publisher.pre_publish_hooks.append(self._on_pre_publish)
        self.publisher.post_publish_hooks.append(self._on_published)

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
        }
        try:
            with open(WEBDAV_CFG_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in cfg:
                    if key in data:
                        cfg[key] = data[key]
        except Exception:
            pass
        return cfg

    def _save_webdav_cfg(self) -> None:
        try:
            with open(WEBDAV_CFG_FILE, "w") as f:
                json.dump(self.webdav_cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_webdav_logs(self) -> dict:
        try:
            with open(WEBDAV_LOGS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_webdav_logs(self) -> None:
        try:
            cutoff = time.time() - WEBDAV_LOG_HOURS * 3600
            self.webdav_logs = {
                k: v
                for k, v in self.webdav_logs.items()
                if isinstance(v, dict) and v.get("ts", 0) >= cutoff
            }
            with open(WEBDAV_LOGS_FILE, "w") as f:
                json.dump(self.webdav_logs, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_webdav_count(self) -> dict:
        try:
            with open(WEBDAV_COUNT_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_webdav_count(self) -> None:
        try:
            # 只保留最近 7 天的计数，避免文件无限增长
            self.webdav_count = {
                k: v
                for k, v in self.webdav_count.items()
                if isinstance(v, int) and k >= (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            }
            with open(WEBDAV_COUNT_FILE, "w") as f:
                json.dump(self.webdav_count, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_proxy_cfg(self) -> dict:
        cfg = {"auto": True, "current": -1, "proxies": []}
        try:
            with open(PROXY_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg["auto"] = bool(data.get("auto", True))
                cfg["current"] = int(data.get("current", -1))
                proxies = data.get("proxies", [])
                cfg["proxies"] = (
                    [p for p in proxies if isinstance(p, dict) and p.get("url")]
                    if isinstance(proxies, list)
                    else []
                )
        except Exception:
            pass
        return cfg

    def _save_proxy_cfg(self) -> None:
        try:
            with open(PROXY_FILE, "w") as f:
                json.dump(self.proxy_cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

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
        return proxies[idx].get("url", f"#{idx + 1}")

    async def _apply_proxy(self, idx: int) -> bool:
        """应用代理（idx=-1 直连）：改 client._proxy + 重建连接，session 保留免重登。"""
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
            self.proxy_cfg["current"] = idx
            self._save_proxy_cfg()
            self.client._proxy = proxy
            try:
                await self.client.disconnect()
            except Exception:
                pass
            await self.client.connect()
            logger.info("Applied proxy #%s (%s)", idx, self._proxy_label(idx))
            return True
        except Exception as exc:
            logger.warning("Proxy switch to #%s failed: %s", idx, exc)
            return False

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
        """下载网络失败时自动切换代理：直连失败→依次试各代理；代理失败→下一个；全败恢复直连。"""
        proxies = self.proxy_cfg.get("proxies", [])
        if not proxies:
            return False
        if not self.proxy_cfg.get("auto"):
            return False
        current = self.proxy_cfg.get("current", -1)
        order = list(range(len(proxies)))
        if current >= 0:
            # 从下一个开始，绕过当前失败的
            order = [i for i in order if i != current]
        for idx in order:
            if await self._apply_proxy(idx):
                logger.info("Job #%s 网络失败，已自动切换代理 #%s", seq, idx)
                return True
        logger.info("Job #%s 所有代理均失败，恢复直连", seq)
        await self._apply_proxy(-1)
        return False

    @staticmethod
    def _test_http_proxy(url: str) -> bool:
        """通过代理访问测试连通性（urllib 标准库）。"""
        import urllib.request

        handler = urllib.request.ProxyHandler({"http": url, "https": url})
        opener = urllib.request.build_opener(handler)
        try:
            with opener.open("https://api.ipify.org", timeout=8) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _proxy_view(self) -> tuple:
        """/proxy 主视图。"""
        cfg = self.proxy_cfg
        current = cfg.get("current", -1)
        label = self._proxy_label(current)
        if current >= 0:
            conn = f"代理 #{current + 1}：{label}"
        else:
            conn = "直连"
        auto = "✅ 已开启" if cfg.get("auto") else "⛔ 已关闭"
        lines = [
            "🌐 代理设置（仅 HTTP 代理）",
            "",
            f"当前连接：{conn}",
            f"自动切换：{auto}",
            f"代理数量：{len(cfg.get('proxies', []))}",
            "",
            "下载网络失败时自动切换到下一个可用代理。",
        ]
        buttons = [
            [Button.inline("➕ 添加代理", "proxy:add")],
            [
                Button.inline(
                    "⛔ 关闭自动切换" if cfg.get("auto") else "🔛 开启自动切换",
                    "proxy:auto",
                )
            ],
            [
                Button.inline("🔀 管理代理", "proxy:list"),
                Button.inline("🔌 直连", "proxy:direct"),
            ],
        ]
        return "\n".join(lines), buttons

    def _proxy_list_view(self) -> tuple:
        """/proxy 管理列表。"""
        proxies = self.proxy_cfg.get("proxies", [])
        current = self.proxy_cfg.get("current", -1)
        lines = ["🔀 代理列表", ""]
        buttons = [[Button.inline("⬅️ 返回", "proxy:back")]]
        if not proxies:
            lines.append("（暂无代理，点 ➕ 添加）")
        for idx, p in enumerate(proxies):
            mark = "✅ " if idx == current else ""
            lines.append(f"{mark}代理 #{idx + 1}：{p.get('url', '')}")
            buttons.append(
                [
                    Button.inline("✅ 使用", f"proxy:use:{idx}"),
                    Button.inline("🧪 测试", f"proxy:test:{idx}"),
                    Button.inline("🗑 删除", f"proxy:del:{idx}"),
                ]
            )
        return "\n".join(lines), buttons

    def _show_progress(self, user_id: int) -> bool:
        return self._get_pref(user_id, "show_progress", True)

    def toggle_progress_pref(self, user_id: int) -> bool:
        current = self._show_progress(user_id)
        self._set_pref(user_id, "show_progress", not current)
        return not current

    def _spoiler_mode(self, user_id: int) -> str:
        # 默认"总是正常"：首次使用不询问，需要雪花遮挡的用户用 /mode 自行设置
        return self._get_pref(user_id, "spoiler_mode", "always_normal")

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
        texts: list = None,
    ) -> None:
        self.pending[seq] = _PendingJob(
            seq=seq, kind=kind, message=message, album=album, user_id=user_id,
            texts=texts,
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
        self.active_seqs.add(seq)
        try:
            await self._auto_enqueue(
                pending.kind,
                pending.message,
                pending.album,
                pending.user_id,
                force_normal=True,
                texts=pending.texts,
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
        """把一批媒体加入合集会话（自动 /begin）：追加 → 显示「🛑 结束并发布」按钮 → 5s 后隐藏。"""
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
        """更新会话状态消息：显示「🛑 结束并发布」按钮并重置 5s 隐藏计时。"""
        if session.button_task is not None:
            session.button_task.cancel()
        summary = f"已收录 {len(session.items)} 项（{session.media_count} 个媒体"
        if session.texts:
            summary += f" · {session.text_count} 条评论"
        summary += "）"
        text = (
            f"📦 合集会话进行中 · {summary}\n"
            "继续转发自动加入合集，点击下方按钮或发 /end 结束并发布"
        )
        buttons = [[Button.inline("🛑 结束并发布", f"session_end:{user_id}")]]
        try:
            if session.status is None:
                session.status = await self.client.send_message(
                    user_id, text, buttons=buttons
                )
            else:
                await session.status.edit(text, buttons=buttons)
        except Exception as exc:
            logger.exception("Session status update failed: %s", exc)
        session.button_task = asyncio.get_running_loop().create_task(
            self._session_button_timeout(user_id, session)
        )

    async def _session_button_timeout(self, user_id: int, session: _Session) -> None:
        await asyncio.sleep(SESSION_END_TIMEOUT)
        if self.sessions.get(user_id) is not session:
            return
        summary = f"已收录 {len(session.items)} 项（{session.media_count} 个媒体"
        if session.texts:
            summary += f" · {session.text_count} 条评论"
        summary += "）"
        try:
            await session.status.edit(
                f"📦 合集会话进行中 · {summary}\n"
                "继续转发自动加入合集，发 /end 结束并发布",
                buttons=None,
            )
        except Exception:
            pass

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
            while self._paused:
                await asyncio.sleep(1)
            job = await self.input_q.get()
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
                    task = asyncio.get_running_loop().create_task(
                        self.downloader.run(job)
                    )
                    self._download_tasks[job.seq] = task
                    done, _ = await asyncio.wait({task}, timeout=DOWNLOAD_TIMEOUT)
                    if task in done:
                        try:
                            path = task.result()
                        except asyncio.CancelledError:
                            logger.info(
                                "Job #%s download stopped by user", job.seq
                            )
                            self._cancel_marked.discard(job.seq)
                            self._set_cancelled(job.seq)
                            await self._delete_status(job)
                        except Exception as exc:
                            if (
                                _is_network_error(exc)
                                and retries < DOWNLOAD_AUTO_RETRY
                            ):
                                switched = await self._try_switch_proxy(job.seq)
                                retries += 1
                                logger.warning(
                                    "Job #%s download failed (%s), auto-retry %d/%d%s",
                                    job.seq,
                                    exc.__class__.__name__,
                                    retries,
                                    DOWNLOAD_AUTO_RETRY,
                                    "（已切换代理）" if switched else "",
                                )
                                await asyncio.sleep(2)
                                continue
                            logger.exception(
                                "Download failed for job #%s", job.seq
                            )
                            self._set_exception(job.seq, exc)
                        else:
                            self._set_result(job.seq, path)
                        break
                    task.cancel()
                    if retries < DOWNLOAD_AUTO_RETRY:
                        retries += 1
                        logger.warning(
                            "Job #%s download timeout, auto-retry %d/%d",
                            job.seq,
                            retries,
                            DOWNLOAD_AUTO_RETRY,
                        )
                        await asyncio.sleep(2)
                        continue
                    logger.error(
                        "Job #%s timed out after %ss", job.seq, DOWNLOAD_TIMEOUT
                    )
                    self._set_exception(
                        job.seq,
                        TimeoutError(f"下载超时（{DOWNLOAD_TIMEOUT} 秒）"),
                    )
                    break
            except asyncio.CancelledError:
                logger.info("Job #%s download worker cancelled", job.seq)
                self._cancel_marked.discard(job.seq)
                self._set_cancelled(job.seq)
                await self._delete_status(job)
            finally:
                self._download_tasks.pop(job.seq, None)
                self._active_downloads = max(0, self._active_downloads - 1)
                self.input_q.task_done()

    async def _on_pre_download(self, job) -> None:
        await self._safe_edit(job, f"🔄 {self.task_label(job.seq)} 正在下载...")

    async def _on_download_progress(
        self, seq: int, received: int, total: int, item: int, items: int
    ) -> None:
        job = self.jobs.get(seq)
        if job is None:
            return
        if items <= 1:
            overall = int(received * 100 / total) if total else 0
        else:
            frac = received / total if total else 0
            overall = round(((item - 1) + frac) * 100 / items)
        self.active[seq] = {
            "phase": "download",
            "pct": overall,
            "item": item,
            "items": items,
            "user_id": job.user_id,
        }
        logger.info(
            "Job #%s download progress: %d/%d (%d%%)", seq, received, total, overall
        )
        await self._update_progress_status(seq)

    async def _on_download_done(self, job, paths) -> None:
        await self._safe_edit(
            job,
            f"✅ 队列第 {self.task_label(job.seq)} 下载完成，等待上传",
            buttons=[
                Button.inline("⏸ 暂停", f"hold:{job.seq}"),
                Button.inline("⏭ 跳过", f"hold:{job.seq}"),
                Button.inline("⏹ 取消", f"q_cancel:{job.seq}"),
            ],
        )

    async def _on_webdav_upload(self, job, paths) -> None:
        """下载完成后后台上传到 WebDAV，不阻塞主流程。

        目录结构：<路径>/<当天日期>/<当天第 N 次上传>/（N 持久化，重启不重置）。
        逐文件记录状态到 webdav_logs（持久化），失败文件保留本地缓存供重试。
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
            "seq": seq,
            "ts": time.time(),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "remote_dir": remote_dir,
            "files": [
                {"name": os.path.basename(p), "local": p, "status": "pending"}
                for p in file_list
            ],
        }

        async def _upload() -> None:
            try:
                for f in log["files"]:
                    if f["status"] == "ok":
                        continue
                    ok = await asyncio.to_thread(
                        webdav.upload_file,
                        cfg.get("url"),
                        remote_dir,
                        f["local"],
                        cfg.get("user"),
                        cfg.get("pass"),
                        int(cfg.get("retry", 2)),
                    )
                    f["status"] = "ok" if ok else "failed"
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
            finally:
                failed = [f for f in log["files"] if f["status"] == "failed"]
                if failed:
                    self.webdav_keep_cache.add(seq)
                else:
                    self.webdav_keep_cache.discard(seq)
                self.webdav_logs[str(seq)] = log
                self._save_webdav_logs()

        task = asyncio.get_running_loop().create_task(_upload())
        self._webdav_tasks[task] = seq

        def _done(t: asyncio.Task) -> None:
            self._webdav_tasks.pop(t, None)
            if t.exception() and not isinstance(t.exception(), asyncio.CancelledError):
                logger.error("Job #%s webdav task failed", seq)

        task.add_done_callback(_done)

    def _webdav_cfg_view(self) -> tuple:
        """按钮式配置主视图（/webdav）：状态卡片 + 3 个入口按钮，避免臃肿。"""
        cfg = self.webdav_cfg
        status = "✅ 已启用" if cfg.get("enabled") else "⛔ 已停用"
        lines = [
            "📁 WebDAV 备份配置（下载完成后自动备份媒体）",
            "",
            f"状态：{status}",
            f"地址：{cfg.get('url') or '（未设置）'}",
            f"账号：{cfg.get('user') or '（未设置）'}",
            f"密码：{'***' if cfg.get('pass') else '（未设置）'}",
            f"路径：{cfg.get('path') or '（未设置）'}",
            f"重试：{cfg.get('retry')}",
        ]
        toggle = (
            Button.inline("⛔ 停用", "wd_cfg:off")
            if cfg.get("enabled")
            else Button.inline("🔛 启用", "wd_cfg:on")
        )
        buttons = [
            [toggle],
            [Button.inline("⚙️ 修改配置", "wd_cfg:edit")],
            [Button.inline("📁 上传记录", "wd_cfg:logs")],
        ]
        return "\n".join(lines), buttons

    def _webdav_cfg_fields_view(self) -> tuple:
        """「修改配置」字段页：一次可连续修改多个字段，完成后返回。"""
        cfg = self.webdav_cfg
        lines = [
            "⚙️ WebDAV 修改配置",
            "",
            f"地址：{cfg.get('url') or '（未设置）'}",
            f"账号：{cfg.get('user') or '（未设置）'}",
            f"密码：{'***' if cfg.get('pass') else '（未设置）'}",
            f"路径：{cfg.get('path') or '（未设置）'}",
            f"重试：{cfg.get('retry')}",
            "",
            "点击按钮，直接回复新值即可：",
        ]
        buttons = [
            [
                Button.inline("✏️ 地址", "wd_cfg:url"),
                Button.inline("✏️ 账号", "wd_cfg:user"),
            ],
            [
                Button.inline("✏️ 密码", "wd_cfg:pass"),
                Button.inline("✏️ 路径", "wd_cfg:path"),
            ],
            [Button.inline("✏️ 重试", "wd_cfg:retry")],
            [Button.inline("⬅️ 返回", "wd_cfg:back")],
        ]
        return "\n".join(lines), buttons

    def _webdav_logs_view(self) -> tuple:
        """/webdav 内「上传记录」视图：最近 24 小时的上传记录 + 每行操作按钮。"""
        self._save_webdav_logs()
        logs = sorted(
            self.webdav_logs.items(),
            key=lambda kv: kv[1].get("ts", 0),
            reverse=True,
        )
        if not logs:
            return (
                "📁 WebDAV 上传记录\n\n（最近 24 小时暂无记录）",
                [[Button.inline("⬅️ 返回", "wd_cfg:back")]],
            )
        lines = ["📁 WebDAV 上传记录（最近 24 小时）", ""]
        buttons = [[Button.inline("⬅️ 返回", "wd_cfg:back")]]
        for index, (key, log) in enumerate(logs):
            files = log.get("files", [])
            total = len(files)
            ok = sum(1 for f in files if f.get("status") == "ok")
            failed = total - ok
            mark = "✅ 全部成功" if failed == 0 else f"⚠️ 失败 {failed}/{total}"
            lines.append(
                f"{_pos_token(index + 1)} {log.get('time', '')}  {mark}\n"
                f"    {log.get('remote_dir', '')}（{ok}/{total}）"
            )
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
        missing = [f["name"] for f in failed if not os.path.isfile(f.get("local", ""))]
        if missing:
            return f"❌ 本地缓存已不存在，无法重试: {', '.join(missing[:3])}"

        async def _upload() -> None:
            try:
                for f in failed:
                    ok = await asyncio.to_thread(
                        webdav.upload_file,
                        cfg.get("url"),
                        log["remote_dir"],
                        f["local"],
                        cfg.get("user"),
                        cfg.get("pass"),
                        int(cfg.get("retry", 2)),
                    )
                    f["status"] = "ok" if ok else "failed"
                    logger.info(
                        "Job #%s webdav retry %s (%s)",
                        log["seq"],
                        f["name"],
                        "OK" if ok else "FAILED",
                    )
            except Exception as exc:
                logger.error("Job #%s webdav retry error: %s", log["seq"], exc)
            finally:
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
        if items <= 1:
            overall = int(received * 100 / total) if total else 0
        else:
            frac = received / total if total else 0
            overall = round(((item - 1) + frac) * 100 / items)
        self.active[seq] = {
            "phase": "upload",
            "pct": overall,
            "item": item,
            "items": items,
            "user_id": job.user_id,
        }
        await self._update_progress_status(seq)

    async def _on_pre_publish(self, job, payload) -> None:
        waiting = sum(1 for s, f in self.results.items() if s != job.seq and f.done())
        suffix = f"（另有 {waiting} 个待上传）" if waiting else ""
        label = "🔞 雪花遮挡" if job.spoiler else ""
        if isinstance(payload, list):
            total = sum(os.path.getsize(p) for p in payload)
            unit = "个媒体" if job.kind == "collection" else "张"
            title = "合集" if job.kind == "collection" else "相册"
            await self._safe_edit(
                job,
                f"✅ {title}下载完成（{len(payload)} {unit}，共 {total / 1024 / 1024:.1f}MB）"
                f"{label}，正在上传{suffix}...",
            )
        else:
            size = os.path.getsize(payload)
            await self._safe_edit(
                job,
                f"✅ 下载完成（{size / 1024 / 1024:.1f}MB）{label}，正在上传{suffix}...",
            )

    async def _on_published(self, job, ids: list) -> None:
        self._remember_published(job.seq, ids)
        if job.kind == "collection":
            text = f"✅ 合集已发布到 {DEST_CHANNEL}"
        elif job.kind == "album":
            text = f"✅ 相册已发布到 {DEST_CHANNEL}"
        else:
            text = f"✅ 已发布到 {DEST_CHANNEL}"
        await self._safe_edit(
            job, text, buttons=[Button.inline("↩️ 撤销", f"undo:{job.seq}")]
        )
        if AUTO_DELETE_SECONDS > 0:
            asyncio.get_running_loop().create_task(
                _delete_after(job.status, AUTO_DELETE_SECONDS)
            )

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
            buttons.append(Button.inline("⏹ 取消", f"stop:{seq}"))
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

    async def _upload_worker(self) -> None:
        watchdog = DOWNLOAD_TIMEOUT + 60
        while True:
            while self._paused:
                await asyncio.sleep(1)
            # 下载优先：有未下载任务（排队中或下载中）→ 挂起上传
            while not self.input_q.empty() or self._active_downloads > 0:
                await asyncio.sleep(0.5)
            seq = self._pick_next_upload()
            if seq is None:
                await self._watchdog_unresolved(watchdog)
                await asyncio.sleep(1)
                continue
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
            task = asyncio.get_running_loop().create_task(
                self.publisher.publish(job, path)
            )
            self._upload_tasks[seq] = task
            try:
                done, _ = await asyncio.wait({task}, timeout=UPLOAD_TIMEOUT)
            except asyncio.CancelledError:
                logger.info("Job #%s upload worker cancelled", seq)
                self._cancel_marked.discard(seq)
                await self._delete_status(job)
                self._finish_seq(seq)
            else:
                if task in done:
                    try:
                        await task
                    except asyncio.CancelledError:
                        logger.info("Job #%s upload stopped by user", seq)
                        self._cancel_marked.discard(seq)
                        await self._delete_status(job)
                        self._finish_seq(seq)
                    except FileTooLargeError as exc:
                        logger.warning("Job #%s %s", seq, exc)
                        if job is not None:
                            await self._safe_edit(job, f"❌ {exc}")
                        self._finish_seq(seq)
                    except Exception as exc:
                        logger.exception("Upload failed for job #%s", seq)
                        await self._reply_error(
                            seq,
                            f"上传失败: {exc}",
                            retry_job=job,
                            retry_path=path if not isinstance(path, list) else "",
                        )
                        self._finish_seq(seq, keep_cache=True)
                    else:
                        self._finish_seq(seq)
                else:
                    task.cancel()
                    logger.error(
                        "Upload for job #%s timed out after %ss",
                        seq,
                        UPLOAD_TIMEOUT,
                    )
                    await self._reply_error(
                        seq,
                        f"上传超时（{UPLOAD_TIMEOUT} 秒）",
                        retry_job=job,
                        retry_path=path if not isinstance(path, list) else "",
                    )
                    self._finish_seq(seq, keep_cache=True)
            finally:
                self._upload_tasks.pop(seq, None)
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
            _do_cleanup()

        try:
            asyncio.get_running_loop().create_task(_delayed())
        except RuntimeError:
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
        texts: list = None,
    ) -> int:
        # 队列级合并：同一用户已有"入队未下载"的相册任务时，追加消息而非新建任务
        if kind == "album" and album:
            existing_seq = self.pending_albums.get(user_id)
            if existing_seq is not None:
                existing = self.album_jobs.get(existing_seq)
                if existing is not None and not existing.started:
                    seen = {m.id for m in (existing.album or [])}
                    added = [m for m in album if getattr(m, "id", None) not in seen]
                    if added:
                        existing.album.extend(added)
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
        job = _Job(
            seq=seq,
            kind=kind,
            status=status,
            message=message,
            album=album,
            spoiler=spoiler,
            user_id=user_id,
            texts=texts,
        )
        if kind == "album":
            self.album_jobs[seq] = job
            self.pending_albums[user_id] = seq
        self.enqueue(job)
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

    async def _reply_error(
        self, seq: int, text: str, retry_job: _Job = None, retry_path: str = ""
    ) -> None:
        job = self.jobs.get(seq)
        if retry_job is not None:
            self.retryable[seq] = _RetryInfo(job=retry_job, path=retry_path)
        buttons = None
        if retry_job is not None:
            buttons = [
                Button.inline("🔄 重试", f"retry:{seq}"),
                Button.inline("⏭ 跳过", f"hold:{seq}"),
                Button.inline("⏹ 取消", f"q_cancel:{seq}"),
            ]
        if job is not None:
            try:
                await job.status.edit(f"❌ {text}", buttons=buttons)
            except Exception:
                pass


def register_handlers(client: TelegramClient):
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
        mode = pipeline._spoiler_mode(event.sender_id)
        await _respond(event, 
            _START_TEXT + f"\n\n当前 18+ 模式：{_MODE_NAMES[mode]}（/mode 可修改）",
            buttons=_reply_keyboard(),
            auto_delete=False,
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

    @client.on(events.NewMessage(pattern="/webdav"))
    async def on_webdav(event: events.NewMessage.Event) -> None:
        logger.info("CMD /webdav from %s", event.sender_id)
        if not _authorized(event):
            return
        cfg = pipeline.webdav_cfg
        parts = event.raw_text.strip().split(maxsplit=2)
        if len(parts) == 1:
            pipeline.webdav_waiting.pop(event.sender_id, None)
            text, buttons = pipeline._webdav_cfg_view()
            await _respond(event, text, buttons=buttons, auto_delete=False)
            return
        key = parts[1].lower()
        val = parts[2] if len(parts) > 2 else ""
        field_map = {
            "on": ("enabled", True),
            "enable": ("enabled", True),
            "off": ("enabled", False),
            "disable": ("enabled", False),
            "url": ("url", val),
            "user": ("user", val),
            "pass": ("pass", val),
            "path": ("path", val),
            "retry": ("retry", int(val) if val.isdigit() else None),
        }
        if key not in field_map:
            await _respond(
                event,
                f"❌ 未知配置项: {key}（可用: on/off/url/user/pass/path/retry）",
                auto_delete=False,
            )
            return
        field, value = field_map[key]
        if value is None:
            await _respond(event, f"❌ 参数无效: /webdav {key} <值>", auto_delete=False)
            return
        if field == "path" and value and not value.startswith("/"):
            value = "/" + value
        cfg[field] = value
        pipeline._save_webdav_cfg()
        status = "✅ 已启用" if cfg.get("enabled") else "⛔ 已停用"
        await _respond(
            event,
            f"✅ 已更新 WebDAV {field}\n当前状态：{status}",
            auto_delete=False,
        )

    @client.on(events.NewMessage(pattern="/proxy"))
    async def on_proxy(event: events.NewMessage.Event) -> None:
        logger.info("CMD /proxy from %s", event.sender_id)
        if not _authorized(event):
            return
        pipeline.proxy_waiting.pop(event.sender_id, None)
        text, buttons = pipeline._proxy_view()
        await _respond(event, text, buttons=buttons, auto_delete=False)

    @client.on(events.NewMessage(pattern=r"/begin|/开始"))
    async def on_begin(event: events.NewMessage.Event) -> None:
        logger.info("CMD /begin from %s", event.sender_id)
        if not _authorized(event):
            return
        session = pipeline.sessions.get(event.sender_id)
        if session is not None:
            text = f"📦 合集会话已在进行中（{len(session.items)} 项，{session.media_count} 个媒体"
            if session.texts:
                text += f"，{session.text_count} 条评论"
            text += "），转发/评论会自动加入，发 /end 结束"
            await _respond(event, text, buttons=_reply_keyboard())
            return
        pipeline.sessions[event.sender_id] = _Session(user_id=event.sender_id)
        await _respond(
            event,
            "✅ 合集会话已开始：后续转发（视频/图片）与文字评论将汇总为一个合集，"
            "评论会按行整合为封面文字与封面一起发送。\n"
            "每次发送后状态消息上会弹出「🛑 结束并发布」，或点输入框上方的「🛑 结束合集」。",
            buttons=_reply_keyboard(),
        )

    @client.on(events.NewMessage(pattern=r"/end|/结束"))
    async def on_end(event: events.NewMessage.Event) -> None:
        logger.info("CMD /end from %s", event.sender_id)
        if not _authorized(event):
            return
        if event.sender_id not in pipeline.sessions:
            await _respond(
                event,
                "当前没有进行中的合集会话（转发内容会自动开始合集）",
                buttons=_reply_keyboard(),
            )
            return
        await _respond(event, "🛑 正在结束合集并发布…", auto_delete=False, buttons=_reply_keyboard())
        try:
            count = await pipeline._session_finalize(event.sender_id, event.chat_id)
            if not count:
                await _respond(event, "合集为空，未发布任何内容")
        except Exception as exc:
            logger.exception("Session end failed: %s", exc)
            await _respond(event, f"结束合集失败: {exc}")

    @client.on(events.NewMessage(pattern="/queue"))
    async def on_queue(event: events.NewMessage.Event) -> None:
        logger.info("CMD /queue from %s", event.sender_id)
        if not _authorized(event):
            return
        lines = ["📋 队列管理", "每个按钮带位置序号，对应上方第 N 位。"]
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
            elif seq in pipeline._paused_files:
                state = f"{pos} ⏸ 已暂停"
            elif seq in pipeline.jobs:
                state = f"{pos} ✅ 等待上传"
            else:
                state = f"{pos} ⏳ 等待下载"
            active_lines.append(state)
            token = _pos_token(pipeline._queue_position(seq))
            if seq in pipeline._paused_files:
                buttons.append(
                    [
                        Button.inline(f"{token} ▶ 继续", f"resume:{seq}"),
                        Button.inline(f"{token} 🗑 删除", f"q_cancel:{seq}"),
                    ]
                )
            else:
                buttons.append(
                    [
                        Button.inline(f"{token} ⏸ 暂停", f"hold:{seq}"),
                        Button.inline(f"{token} ⏹ 取消", f"q_cancel:{seq}"),
                    ]
                )

        if active_lines:
            lines.append(f"\n▶ 进行中（{len(active_lines)}）")
            lines.extend(active_lines)
        else:
            lines.append("\n▶ 进行中：无")

        pending_lines = []
        for idx, seq in enumerate(sorted(pipeline.pending), start=1):
            p = pipeline.pending[seq]
            kind_label = "相册" if p.kind == "album" else "媒体"
            pending_lines.append(f"❓{_pos_token(idx)} 待确认（{kind_label}）")
            buttons.append(
                [Button.inline(f"{_pos_token(idx)} ❌ 取消", f"cancel:{seq}")]
            )
        if pending_lines:
            lines.append("\n❓ 待确认")
            lines.extend(pending_lines)

        if pipeline.sessions:
            total = sum(s.media_count for s in pipeline.sessions.values())
            texts = sum(s.text_count for s in pipeline.sessions.values())
            lines.append(
                f"\n📦 合集会话进行中：{total} 个媒体 · {texts} 条评论已收录"
                "（发 /end 结束并发布）"
            )

        buttons.append(
            [
                Button.inline("⏸ 全局暂停", "q_pause"),
                Button.inline("▶ 全局恢复", "q_resume"),
            ]
        )
        await _respond(
            event, "\n".join(lines), buttons=buttons, auto_delete=True
        )

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

        if data_text.startswith("wd_cfg:"):
            field = data_text.split(":", 1)[1]
            cfg = pipeline.webdav_cfg
            if field == "logs":
                await _answer("上传记录")
                text, buttons = pipeline._webdav_logs_view()
                try:
                    await event.edit(text, buttons=buttons)
                except Exception:
                    pass
                return
            if field == "edit":
                await _answer("修改配置")
                text, buttons = pipeline._webdav_cfg_fields_view()
                try:
                    await event.edit(text, buttons=buttons)
                except Exception:
                    pass
                return
            if field == "back":
                text, buttons = pipeline._webdav_cfg_view()
                try:
                    await event.edit(text, buttons=buttons)
                except Exception:
                    pass
                return
            if field in ("on", "off"):
                cfg["enabled"] = field == "on"
                pipeline._save_webdav_cfg()
                await _answer("✅ 已启用" if field == "on" else "⛔ 已停用")
                text, buttons = pipeline._webdav_cfg_view()
                try:
                    await event.edit(text, buttons=buttons)
                except Exception:
                    pass
                return
            if field == "cancel":
                pipeline.webdav_waiting.pop(event.sender_id, None)
                await _answer("已取消")
                return
            if field in ("url", "user", "pass", "path", "retry"):
                pipeline.webdav_waiting[event.sender_id] = field
                current = cfg.get(field)
                if field == "pass":
                    current = "***" if current else "（空）"
                await _answer("请直接回复新值")
                try:
                    await event.edit(
                        f"✏️ 请输入新的 WebDAV {field}"
                        f"（当前：{current or '（空）'}）\n"
                        f"直接回复即可；回复 /取消 取消修改",
                        buttons=[Button.inline("❌ 取消", "wd_cfg:cancel")],
                    )
                except Exception:
                    pass
                return
            await _answer("无效操作")
            return

        if data_text.startswith("wd_retry:"):
            key = data_text.split(":", 1)[1]
            await _answer(await pipeline._webdav_retry(key))
            text, buttons = pipeline._webdav_logs_view()
            try:
                await event.edit(text, buttons=buttons)
            except Exception:
                pass
            return

        if data_text.startswith("wd_del:"):
            key = data_text.split(":", 1)[1]
            await _answer(await pipeline._webdav_delete(key))
            text, buttons = pipeline._webdav_logs_view()
            try:
                await event.edit(text, buttons=buttons)
            except Exception:
                pass
            return

        if data_text.startswith("proxy:"):
            field = data_text.split(":", 1)[1]

            async def _refresh(view_text, view_buttons) -> None:
                try:
                    await event.edit(view_text, buttons=view_buttons)
                except Exception:
                    pass

            if field == "add":
                pipeline.proxy_waiting[event.sender_id] = "add"
                await _answer("请输入代理地址")
                await _refresh(
                    "➕ 请输入 HTTP 代理地址（直接回复即可）：\n\n"
                    "格式：\n"
                    "  http://host:port\n"
                    "  http://user:pass@host:port\n\n"
                    "回复 /取消 取消添加",
                    [Button.inline("❌ 取消", "proxy:cancel")],
                )
                return
            if field == "cancel":
                pipeline.proxy_waiting.pop(event.sender_id, None)
                await _answer("已取消")
                text, buttons = pipeline._proxy_view()
                await _refresh(text, buttons)
                return
            if field == "auto":
                pipeline.proxy_cfg["auto"] = not pipeline.proxy_cfg.get("auto", True)
                pipeline._save_proxy_cfg()
                await _answer("自动切换已开启" if pipeline.proxy_cfg["auto"] else "自动切换已关闭")
                text, buttons = pipeline._proxy_view()
                await _refresh(text, buttons)
                return
            if field == "direct":
                ok = await pipeline._apply_proxy(-1)
                await _answer("已切回直连" if ok else "切换失败")
                text, buttons = pipeline._proxy_view()
                await _refresh(text, buttons)
                return
            if field == "list":
                text, buttons = pipeline._proxy_list_view()
                await _refresh(text, buttons)
                return
            if field == "back":
                text, buttons = pipeline._proxy_view()
                await _refresh(text, buttons)
                return
            if field.startswith("use:"):
                try:
                    idx = int(field.split(":", 1)[1])
                except (ValueError, IndexError):
                    await _answer("无效操作")
                    return
                ok = await pipeline._apply_proxy(idx)
                await _answer("✅ 已切换" if ok else "❌ 切换失败")
                text, buttons = pipeline._proxy_view()
                await _refresh(text, buttons)
                return
            if field.startswith("test:"):
                try:
                    idx = int(field.split(":", 1)[1])
                except (ValueError, IndexError):
                    await _answer("无效操作")
                    return
                proxies = pipeline.proxy_cfg.get("proxies", [])
                if idx >= len(proxies):
                    await _answer("代理不存在")
                    return
                url = proxies[idx].get("url", "")
                await _answer("⏳ 测试中…")
                ok = await asyncio.to_thread(pipeline._test_http_proxy, url)
                await _answer("✅ 代理可用" if ok else "❌ 代理不可用")
                return
            if field.startswith("del:"):
                try:
                    idx = int(field.split(":", 1)[1])
                except (ValueError, IndexError):
                    await _answer("无效操作")
                    return
                proxies = pipeline.proxy_cfg.get("proxies", [])
                if idx >= len(proxies):
                    await _answer("代理不存在")
                    return
                removed = proxies.pop(idx)
                current = pipeline.proxy_cfg.get("current", -1)
                if current == idx:
                    pipeline.proxy_cfg["current"] = -1
                    await pipeline._apply_proxy(-1)
                elif current > idx:
                    pipeline.proxy_cfg["current"] = current - 1
                pipeline._save_proxy_cfg()
                await _answer(f"已删除代理 {removed.get('url', '')}")
                text, buttons = pipeline._proxy_list_view()
                await _refresh(text, buttons)
                return
            await _answer("无效操作")
            return

        if data_text.startswith("session_end:"):
            try:
                target = int(data_text.split(":", 1)[1])
            except (ValueError, IndexError):
                await _answer("无效操作")
                return
            if target != event.sender_id:
                await _answer("无权限")
                return
            if target not in pipeline.sessions:
                await _answer("当前没有进行中的合集")
                return
            await _answer("已结束，正在处理…")
            try:
                await pipeline._session_finalize(target, event.chat_id)
            except Exception as exc:
                logger.exception("Session end callback failed: %s", exc)
                await _answer(f"结束失败: {exc}")
            return

        if data_text.startswith("mode:"):
            mode = data_text.split(":", 1)[1]
            if mode not in _MODE_NAMES:
                await _answer("无效操作")
                return
            pipeline.set_spoiler_mode(event.sender_id, mode)
            await _answer(f"已设置：{_MODE_NAMES[mode]}")
            try:
                await event.edit(f"✅ 已设置 18+ 模式：{_MODE_NAMES[mode]}")
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
                if ids and isinstance(ids[0], tuple):
                    for peer, mid in ids:
                        await event.client.delete_messages(peer, mid)
                else:
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

        if data_text.startswith("hold:"):
            try:
                seq = int(data_text.split(":", 1)[1])
            except (ValueError, IndexError):
                await _answer("无效操作")
                return
            pipeline._paused_files.add(seq)
            job = pipeline.jobs.get(seq)
            if job is not None:
                try:
                    await job.status.edit(
                        f"⏸ 队列第 {pipeline.task_label(seq)} 已暂停（缓存保留）",
                        buttons=[
                            Button.inline("▶ 继续", f"resume:{seq}"),
                            Button.inline("🗑 删除", f"q_cancel:{seq}"),
                        ],
                    )
                except Exception:
                    pass
            await _answer("已暂停")
            return

        if data_text.startswith("resume:"):
            try:
                seq = int(data_text.split(":", 1)[1])
            except (ValueError, IndexError):
                await _answer("无效操作")
                return
            pipeline._paused_files.discard(seq)
            job = pipeline.jobs.get(seq)
            if job is not None:
                try:
                    await job.status.edit(
                        f"🔄 队列第 {pipeline.task_label(seq)} 已继续，等待上传",
                        buttons=[
                            Button.inline("⏸ 暂停", f"hold:{seq}"),
                            Button.inline("⏭ 跳过", f"hold:{seq}"),
                            Button.inline("⏹ 取消", f"q_cancel:{seq}"),
                        ],
                    )
                except Exception:
                    pass
            await _answer("已继续")
            return

        if data_text.startswith("retry:"):
            try:
                seq = int(data_text.split(":", 1)[1])
            except (ValueError, IndexError):
                await _answer("无效操作")
                return
            info = pipeline.retryable.pop(seq, None)
            if info is None:
                await _answer("该任务已失效（可能已重试）")
                return
            new_seq = pipeline.reserve_seq()
            cached = info.path or ""
            cleanup = os.path.dirname(cached) if cached else ""
            new_job = _Job(
                seq=new_seq,
                kind=info.job.kind,
                status=info.job.status,
                message=info.job.message,
                album=info.job.album,
                url=info.job.url,
                spoiler=info.job.spoiler,
                user_id=info.job.user_id,
                cached_path=cached,
                cleanup_extra=cleanup,
                texts=info.job.texts,
            )
            pipeline.active_seqs.add(new_seq)
            try:
                new_status = await event.client.send_message(
                    event.chat_id,
                    f"🔄 {pipeline.task_label(new_seq)} 已重新入队",
                )
            except Exception:
                new_status = info.job.status
            new_job.status = new_status
            pipeline.enqueue(new_job)
            try:
                await event.edit("🔄 已重新入队")
            except Exception:
                pass
            await _answer("已重新入队")
            logger.info("Job #%s retried as #%s (cached=%s)", seq, new_seq, bool(cached))
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
                    texts=pending.texts,
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

        if event.sender_id in pipeline.webdav_waiting:
            field = pipeline.webdav_waiting.get(event.sender_id)
            text = (event.raw_text or "").strip()
            if text in ("/取消", "/cancel"):
                pipeline.webdav_waiting.pop(event.sender_id, None)
                await _respond(event, "❌ 已取消修改", auto_delete=False)
                return
            cfg = pipeline.webdav_cfg
            value = text
            if field == "retry":
                if not value.isdigit():
                    await _respond(
                        event, "❌ 重试次数必须是数字，请重新输入", auto_delete=False
                    )
                    return
                value = int(value)
                if value < 0 or value > 10:
                    await _respond(
                        event, "❌ 重试次数需在 0-10 之间，请重新输入", auto_delete=False
                    )
                    return
            elif field == "url":
                if not re.match(r"^https?://", value, re.IGNORECASE):
                    await _respond(
                        event,
                        "❌ 地址需以 http:// 或 https:// 开头，请重新输入",
                        auto_delete=False,
                    )
                    return
            elif field == "path":
                if value and not value.startswith("/"):
                    value = "/" + value
            if field in ("url", "user", "pass", "path") and not value:
                await _respond(event, "❌ 内容不能为空，请重新输入", auto_delete=False)
                return
            cfg[field] = value
            pipeline._save_webdav_cfg()
            pipeline.webdav_waiting.pop(event.sender_id, None)
            shown = "***" if field == "pass" else value
            await _respond(
                event, f"✅ 已更新 WebDAV {field}：{shown}", auto_delete=False
            )
            text, buttons = pipeline._webdav_cfg_fields_view()
            await _respond(event, text, buttons=buttons, auto_delete=False)
            return

        if event.sender_id in pipeline.proxy_waiting:
            text = (event.raw_text or "").strip()
            if text in ("/取消", "/cancel"):
                pipeline.proxy_waiting.pop(event.sender_id, None)
                await _respond(event, "❌ 已取消添加代理", auto_delete=False)
                text, buttons = pipeline._proxy_view()
                await _respond(event, text, buttons=buttons, auto_delete=False)
                return
            pipeline.proxy_waiting.pop(event.sender_id, None)
            parsed = pipeline._parse_proxy_url(text)
            if parsed is None:
                await _respond(
                    event,
                    "❌ 代理格式无效，应形如 http://host:port 或 http://user:pass@host:port\n"
                    "重新发 /proxy 再试",
                    auto_delete=False,
                )
                return
            if any(
                p.get("url") == text for p in pipeline.proxy_cfg.get("proxies", [])
            ):
                await _respond(event, "⚠️ 该代理已存在", auto_delete=False)
                text, buttons = pipeline._proxy_view()
                await _respond(event, text, buttons=buttons, auto_delete=False)
                return
            ok = await asyncio.to_thread(pipeline._test_http_proxy, text)
            if not ok:
                await _respond(
                    event,
                    "⚠️ 该代理测试连通失败，仍要添加请确认代理可用；已跳过添加",
                    auto_delete=False,
                )
                text, buttons = pipeline._proxy_view()
                await _respond(event, text, buttons=buttons, auto_delete=False)
                return
            pipeline.proxy_cfg.setdefault("proxies", []).append({"url": text})
            pipeline._save_proxy_cfg()
            await _respond(event, f"✅ 已添加代理：{text}", auto_delete=False)
            text, buttons = pipeline._proxy_view()
            await _respond(event, text, buttons=buttons, auto_delete=False)
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
            if SESSION_COLLECT:
                try:
                    await pipeline._session_add_batch(
                        event.sender_id, [event.message], event.chat_id
                    )
                except Exception as exc:
                    logger.exception("Add single media to session failed: %s", exc)
                    await _respond(event, f"加入合集失败: {exc}")
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

        text = (event.raw_text or "").strip()
        if text == _SESSION_BTN_BEGIN:
            await on_begin(event)
            return
        if text == _SESSION_BTN_END:
            await on_end(event)
            return
        if SESSION_COLLECT and text and pipeline.sessions.get(event.sender_id) is not None:
            try:
                await pipeline._session_add_text(
                    event.sender_id, text, event.chat_id
                )
            except Exception as exc:
                logger.exception("Add text comment to session failed: %s", exc)
                await _respond(event, f"添加评论失败: {exc}")
            return

        await _respond(event, "请发送视频或链接，或使用 /start 查看使用说明。")

    return pipeline
