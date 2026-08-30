"""Safe error classification and stage-specific retry decisions.

This module intentionally does not import Telegram, yt-dlp or WebDAV clients so
the policy remains testable and usable by every transport adapter.
"""

from __future__ import annotations

import asyncio
import errno
import random
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class ErrorCode(str, Enum):
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_UNREACHABLE = "network_unreachable"
    TELEGRAM_FLOOD_WAIT = "telegram_flood_wait"
    TELEGRAM_AUTH = "telegram_auth"
    TELEGRAM_PERMISSION = "telegram_permission"
    SOURCE_EXPIRED = "source_expired"
    URL_UNSUPPORTED = "url_unsupported"
    FILE_TOO_LARGE = "file_too_large"
    DISK_LOW = "disk_low"
    CACHE_MISSING = "cache_missing"
    MEDIA_INVALID = "media_invalid"
    PUBLISH_PARTIAL = "publish_partial"
    WEBDAV_AUTH = "webdav_auth"
    WEBDAV_NOT_FOUND = "webdav_not_found"
    WEBDAV_LOCKED = "webdav_locked"
    WEBDAV_SERVER = "webdav_server"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ErrorInfo:
    code: ErrorCode
    summary: str
    retryable: bool
    suggested_action: str
    retry_after: float | None = None


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    attempt: int
    budget: int
    delay_seconds: float | None = None
    next_retry_at: float | None = None


_AUTH_NAMES = {"AuthKeyError", "AuthKeyUnregisteredError", "SessionRevokedError", "UnauthorizedError"}
_PERMISSION_NAMES = {"ChatAdminRequiredError", "ChatWriteForbiddenError", "ChannelPrivateError", "ForbiddenError"}
_EXPIRED_NAMES = {"MessageIdInvalidError", "MediaEmptyError", "FileReferenceExpiredError", "FileReferenceEmptyError"}
_NETWORK_NAMES = {"TimedOutError", "ServerError", "RpcCallFailError", "RpcMcgetFailError", "NetworkError"}


def classify_error(exc: BaseException, *, stage: str = "unknown") -> ErrorInfo:
    """Map arbitrary transport exceptions to safe, user-facing domain errors."""
    name = exc.__class__.__name__
    lowered = str(exc).lower()
    status = _http_status(exc)

    if isinstance(exc, asyncio.CancelledError) or name == "UrlDownloadCancelled":
        return ErrorInfo(ErrorCode.CANCELLED, "任务已取消", False, "无需处理")
    if name == "FloodWaitError" or hasattr(exc, "seconds") and "flood" in name.lower():
        seconds = max(0.0, float(getattr(exc, "seconds", 0) or 0))
        return ErrorInfo(ErrorCode.TELEGRAM_FLOOD_WAIT, "Telegram 请求过于频繁", True, "等待限流解除", seconds)
    if name == "FileTooLargeError" or "file is larger than" in lowered:
        return ErrorInfo(ErrorCode.FILE_TOO_LARGE, "文件超过当前发布上限", False, "调整大小限制或压缩文件")
    if name in _AUTH_NAMES:
        return ErrorInfo(ErrorCode.TELEGRAM_AUTH, "Telegram 登录状态失效", False, "重新登录并检查 session")
    if name in _PERMISSION_NAMES:
        return ErrorInfo(ErrorCode.TELEGRAM_PERMISSION, "目标频道权限不足", False, "检查机器人或账号的频道权限")
    if name in _EXPIRED_NAMES or "file reference" in lowered and "expired" in lowered:
        return ErrorInfo(ErrorCode.SOURCE_EXPIRED, "源消息或媒体引用已失效", False, "重新转发源消息")
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return ErrorInfo(ErrorCode.DISK_LOW, "服务器磁盘空间不足", True, "清理缓存或扩容后重试")
    if isinstance(exc, FileNotFoundError) and stage == "publish":
        return ErrorInfo(ErrorCode.CACHE_MISSING, "发布所需的本地缓存不存在", False, "重新下载源文件")
    if status in {401, 403} and stage == "backup":
        return ErrorInfo(ErrorCode.WEBDAV_AUTH, "WebDAV 认证失败", False, "检查账号和密码")
    if status == 404 and stage == "backup":
        return ErrorInfo(ErrorCode.WEBDAV_NOT_FOUND, "WebDAV 路径不存在", False, "检查远端路径")
    if status == 423 and stage == "backup":
        return ErrorInfo(ErrorCode.WEBDAV_LOCKED, "WebDAV 文件暂时被锁定", True, "稍后重试")
    if status is not None and status >= 500 and stage == "backup":
        return ErrorInfo(ErrorCode.WEBDAV_SERVER, "WebDAV 服务暂时不可用", True, "等待服务恢复")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or name in {"TimedOutError", "ReadTimeout", "ConnectTimeout"}:
        return ErrorInfo(ErrorCode.NETWORK_TIMEOUT, "网络请求超时", True, "稍后重试或检查代理")
    if isinstance(exc, (ConnectionError, OSError)) or name in _NETWORK_NAMES:
        return ErrorInfo(ErrorCode.NETWORK_UNREACHABLE, "网络连接不可用", True, "检查网络或切换代理")
    if stage == "download" and any(token in lowered for token in ("unsupported url", "no suitable extractor", "unsupported site")):
        return ErrorInfo(ErrorCode.URL_UNSUPPORTED, "暂不支持此链接", False, "更新 yt-dlp 或更换来源")
    if stage == "publish" and "partial" in lowered:
        return ErrorInfo(ErrorCode.PUBLISH_PARTIAL, "部分媒体已发布", False, "检查已发布消息后继续或撤销")
    if stage in {"download", "publish"} and any(token in lowered for token in ("invalid media", "failed to parse", "ffmpeg")):
        return ErrorInfo(ErrorCode.MEDIA_INVALID, "媒体文件无效或不兼容", False, "检查文件或执行兼容性处理")
    return ErrorInfo(ErrorCode.UNKNOWN, "处理时发生未知错误", True, "有限重试；持续失败请导出诊断")


def safe_traceback(exc: BaseException) -> str:
    """Return stack locations without exception text, URLs, captions or credentials."""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    if not frames:
        return "<no traceback>"
    return " <- ".join(f"{frame.filename}:{frame.lineno}:{frame.name}" for frame in frames)


class RetryPolicy:
    """Compute bounded exponential backoff; ``attempt`` is one-based."""

    DEFAULT_BUDGETS = {"download": 3, "publish": 2, "backup": 5}

    def __init__(
        self,
        *,
        budgets: dict[str, int] | None = None,
        jitter: Callable[[], float] | None = None,
        flood_safety_seconds: float = 1.0,
    ) -> None:
        self.budgets = {**self.DEFAULT_BUDGETS, **(budgets or {})}
        self._jitter = jitter or random.random
        self.flood_safety_seconds = max(0.0, flood_safety_seconds)

    def decide(self, error: ErrorInfo, *, stage: str, attempt: int, now: float) -> RetryDecision:
        budget = max(0, int(self.budgets.get(stage, 0)))
        if not error.retryable or attempt > budget:
            return RetryDecision(False, attempt, budget)
        if error.code == ErrorCode.TELEGRAM_FLOOD_WAIT:
            delay = max(0.0, float(error.retry_after or 0)) + self.flood_safety_seconds
        else:
            base, cap = (60.0, 3600.0) if stage == "backup" else (5.0, 300.0)
            delay = min(cap, base * (2 ** max(0, attempt - 1))) + max(0.0, self._jitter())
        return RetryDecision(True, attempt, budget, delay, now + delay)


def _http_status(exc: BaseException) -> int | None:
    for value in (getattr(exc, "status", None), getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None)):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return None
