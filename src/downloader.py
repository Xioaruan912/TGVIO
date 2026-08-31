from __future__ import annotations

import asyncio
import logging
import os
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from yt_dlp import YoutubeDL

from .security import enforce_url_policy

logger = logging.getLogger(__name__)


class UrlDownloadCancelled(Exception):
    """Raised inside the yt-dlp worker when cooperative cancellation is requested."""


@dataclass(frozen=True)
class DownloadProgress:
    status: str
    downloaded_bytes: int
    total_bytes: int | None
    speed_bps: float | None
    eta_seconds: float | None
    filename: str | None
    item_index: int = 1
    item_total: int = 1


@dataclass(frozen=True)
class DownloadResult:
    path: str
    title: str


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise UrlDownloadCancelled("URL download cancelled")


ProgressCallback = Callable[[DownloadProgress], None]


class UrlDownloader:
    """Async adapter around yt-dlp's Python API with cooperative cancellation."""

    def __init__(
        self,
        *,
        cancel_wait_seconds: float = 10.0,
        private_network_policy: str = "warn",
    ) -> None:
        self.cancel_wait_seconds = max(0.1, float(cancel_wait_seconds))
        self.private_network_policy = str(private_network_policy or "warn").lower()

    async def download(
        self,
        url: str,
        download_dir: str,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_token: CancelToken | None = None,
    ) -> DownloadResult:
        token = cancel_token or CancelToken()
        loop = asyncio.get_running_loop()

        def emit(progress: DownloadProgress) -> None:
            if on_progress is not None:
                loop.call_soon_threadsafe(on_progress, progress)

        worker = loop.run_in_executor(
            None,
            _download_sync,
            url,
            download_dir,
            token,
            emit,
            self.private_network_policy,
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            token.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=self.cancel_wait_seconds)
            except UrlDownloadCancelled:
                pass
            except asyncio.TimeoutError:
                logger.error("yt-dlp worker did not stop within %.1fs after cancellation", self.cancel_wait_seconds)
            except Exception:
                if not token.cancelled:
                    raise
            raise


async def download_video(
    url: str,
    download_dir: str,
    *,
    on_progress: ProgressCallback | None = None,
    cancel_token: CancelToken | None = None,
    private_network_policy: str = "warn",
) -> tuple[str, str]:
    result = await UrlDownloader(private_network_policy=private_network_policy).download(
        url,
        download_dir,
        on_progress=on_progress,
        cancel_token=cancel_token,
    )
    return result.path, result.title


def _download_sync(
    url: str,
    download_dir: str,
    cancel_token: CancelToken,
    emit: ProgressCallback,
    private_network_policy: str = "warn",
) -> DownloadResult:
    root = Path(download_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    risk = enforce_url_policy(url, private_network_policy=private_network_policy)
    if risk.private_network and private_network_policy == "warn":
        logger.warning("URL download targets private/local network host=%s", risk.hostname)
    elif not risk.addresses and private_network_policy == "warn":
        logger.warning("URL private-network risk unresolved locally host=%s", risk.hostname)

    def progress_hook(data: dict) -> None:
        cancel_token.raise_if_cancelled()
        status = str(data.get("status") or "downloading")
        emit(
            DownloadProgress(
                status=status,
                downloaded_bytes=int(data.get("downloaded_bytes") or 0),
                total_bytes=_optional_int(data.get("total_bytes") or data.get("total_bytes_estimate")),
                speed_bps=_optional_float(data.get("speed")),
                eta_seconds=_optional_float(data.get("eta")),
                filename=_safe_filename(data.get("filename"), root),
            )
        )

    def postprocessor_hook(data: dict) -> None:
        cancel_token.raise_if_cancelled()
        status = str(data.get("status") or "processing")
        emit(
            DownloadProgress(
                status="postprocessing" if status in {"started", "processing"} else status,
                downloaded_bytes=0,
                total_bytes=None,
                speed_bps=None,
                eta_seconds=None,
                filename=_safe_filename(data.get("filepath") or data.get("filename"), root),
            )
        )

    opts = {
        "outtmpl": str(root / "%(id).80s-%(title).120s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "restrictfilenames": True,
        "continuedl": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }

    cancel_token.raise_if_cancelled()
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            cancel_token.raise_if_cancelled()
            title = str(info.get("title") or url)
            path = _resolve_path(ydl, info, root)
    except Exception as exc:
        if cancel_token.cancelled or _contains_cancel(exc):
            raise UrlDownloadCancelled("URL download cancelled") from exc
        raise

    parsed = urllib.parse.urlsplit(url)
    logger.info("Downloaded URL host=%s -> %s", parsed.hostname or "unknown", path)
    return DownloadResult(path=path, title=title)


def _resolve_path(ydl: YoutubeDL, info: dict, root: Path) -> str:
    candidates: list[str] = []
    for key in ("filepath", "_filename"):
        value = info.get(key)
        if value:
            candidates.append(str(value))
    try:
        prepared = ydl.prepare_filename(info)
        if prepared:
            candidates.append(prepared)
    except Exception:
        pass
    for item in info.get("requested_downloads") or []:
        if not isinstance(item, dict):
            continue
        for key in ("filepath", "filename"):
            value = item.get(key)
            if value:
                candidates.append(str(value))

    expanded: list[str] = []
    for candidate in candidates:
        expanded.append(candidate)
        stem, _ext = os.path.splitext(candidate)
        expanded.extend([f"{stem}.mp4", f"{stem}.mkv", f"{stem}.webm"])

    seen: set[str] = set()
    for candidate in expanded:
        resolved = _validated_path(candidate, root)
        if resolved in seen:
            continue
        seen.add(resolved)
        if os.path.isfile(resolved):
            return resolved
    raise FileNotFoundError("下载后未找到 yt-dlp 明确返回的媒体文件")


def _validated_path(path: str, root: Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if os.path.commonpath([str(root), str(resolved)]) != str(root):
        raise ValueError("yt-dlp output escaped job directory")
    return str(resolved)


def _safe_filename(value: object, root: Path) -> str | None:
    if not value:
        return None
    try:
        return os.path.basename(_validated_path(str(value), root))
    except Exception:
        return None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _contains_cancel(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, UrlDownloadCancelled):
            return True
        current = current.__cause__ or current.__context__
    return False
