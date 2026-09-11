from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import threading
from typing import Callable

from yt_dlp import YoutubeDL

from tgvio.application.ports import TransferProgressCallback
from tgvio.domain.job import MediaItem
from tgvio.infrastructure.url_security import validate_download_url


class UrlDownloadCancelled(RuntimeError):
    pass


class UrlMediaDownloader:
    """yt-dlp adapter with contained output paths and cooperative shutdown."""

    def __init__(
        self,
        *,
        private_network_policy: str = "block",
        ydl_factory: Callable = YoutubeDL,
        cancel_wait_seconds: float = 10.0,
    ) -> None:
        self._private_network_policy = private_network_policy
        self._ydl_factory = ydl_factory
        self._cancel_wait_seconds = max(0.1, float(cancel_wait_seconds))

    async def download(
        self,
        item: MediaItem,
        target_dir: Path,
        progress_callback: TransferProgressCallback | None = None,
    ) -> MediaItem:
        if str(item.metadata.get("source_type") or "") != "url":
            raise ValueError("URL downloader received non-URL media item")
        url = self._source_url(item)
        target_dir.mkdir(parents=True, exist_ok=True)
        cancel_event = threading.Event()
        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(
            None,
            self._download_sync,
            item,
            url,
            target_dir.resolve(),
            cancel_event,
            loop,
            progress_callback,
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=self._cancel_wait_seconds)
            except Exception:
                pass
            raise

    def _download_sync(
        self,
        item: MediaItem,
        url: str,
        root: Path,
        cancel_event: threading.Event,
        loop: asyncio.AbstractEventLoop,
        progress_callback: TransferProgressCallback | None,
    ) -> MediaItem:
        risk = validate_download_url(
            url,
            private_network_policy=self._private_network_policy,
        )

        def progress_hook(data: dict) -> None:
            if cancel_event.is_set():
                raise UrlDownloadCancelled("URL download cancelled")
            if progress_callback is not None:
                downloaded = int(data.get("downloaded_bytes") or 0)
                total_raw = data.get("total_bytes") or data.get("total_bytes_estimate")
                total = int(total_raw) if total_raw else None
                loop.call_soon_threadsafe(progress_callback, downloaded, total)

        options = {
            "outtmpl": str(root / "%(id).80s-%(title).120s.%(ext)s"),
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 3,
            "continuedl": True,
            "restrictfilenames": True,
            "progress_hooks": [progress_hook],
        }
        if cancel_event.is_set():
            raise UrlDownloadCancelled("URL download cancelled")
        try:
            with self._ydl_factory(options) as ydl:
                info = ydl.extract_info(url, download=True)
                if cancel_event.is_set():
                    raise UrlDownloadCancelled("URL download cancelled")
                path = self._resolve_path(ydl, info, root)
                title = str(info.get("title") or path.stem)
                extractor = str(info.get("extractor_key") or info.get("extractor") or "generic")
        except UrlDownloadCancelled:
            raise
        except Exception:
            # yt-dlp errors can embed the full source URL. Do not persist or log
            # those strings through the durable Job error path.
            raise RuntimeError("URL download failed") from None

        metadata = dict(item.metadata)
        metadata.update(
            {
                "download_complete": True,
                "download_reused_local": False,
                "url_hostname": risk.hostname,
                "url_title": title[:500],
                "url_extractor": extractor[:120],
            }
        )
        return replace(
            item,
            local_path=str(path),
            name=path.name,
            size_bytes=path.stat().st_size,
            metadata=metadata,
        )

    @staticmethod
    def _source_url(item: MediaItem) -> str:
        prefix = "url:"
        if not item.source.startswith(prefix):
            raise ValueError("URL item has invalid durable source")
        return item.source[len(prefix) :]

    @classmethod
    def _resolve_path(cls, ydl, info: dict, root: Path) -> Path:
        candidates: list[str] = []
        for key in ("filepath", "_filename"):
            value = info.get(key)
            if value:
                candidates.append(str(value))
        try:
            prepared = ydl.prepare_filename(info)
            if prepared:
                candidates.append(str(prepared))
        except Exception:
            pass
        for current in info.get("requested_downloads") or []:
            if not isinstance(current, dict):
                continue
            for key in ("filepath", "filename"):
                value = current.get(key)
                if value:
                    candidates.append(str(value))

        expanded: list[str] = []
        for candidate in candidates:
            expanded.append(candidate)
            stem, _ext = os.path.splitext(candidate)
            expanded.extend((f"{stem}.mp4", f"{stem}.mkv", f"{stem}.webm"))
        seen: set[Path] = set()
        for candidate in expanded:
            resolved = cls._contained_path(candidate, root)
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file() and resolved.stat().st_size > 0:
                return resolved
        raise FileNotFoundError("yt-dlp did not produce a contained media file")

    @staticmethod
    def _contained_path(value: str, root: Path) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            raise ValueError("yt-dlp output escaped job directory")
        return resolved
