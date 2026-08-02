import asyncio
import logging
import os

from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)


async def download_video(url: str, download_dir: str) -> tuple[str, str]:
    return await asyncio.to_thread(_download_sync, url, download_dir)


def _download_sync(url: str, download_dir: str) -> tuple[str, str]:
    os.makedirs(download_dir, exist_ok=True)
    _clear_dir(download_dir)

    opts = {
        "outtmpl": os.path.join(download_dir, "%(title)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "restrictfilenames": True,
    }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title") or url
        path = _resolve_path(ydl, info, download_dir)

    logger.info("Downloaded %s -> %s", url, path)
    return path, title


def _resolve_path(ydl: YoutubeDL, info: dict, download_dir: str) -> str:
    preferred = ydl.prepare_filename(info)
    stem = preferred.rsplit(".", 1)[0]
    for candidate in (preferred, f"{stem}.mp4", f"{stem}.mkv"):
        if os.path.isfile(candidate):
            return candidate

    files = [
        os.path.join(download_dir, f)
        for f in os.listdir(download_dir)
        if os.path.isfile(os.path.join(download_dir, f))
    ]
    if files:
        return max(files, key=os.path.getmtime)

    raise FileNotFoundError("下载后未找到视频文件")


def _clear_dir(download_dir: str) -> None:
    for f in os.listdir(download_dir):
        path = os.path.join(download_dir, f)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
