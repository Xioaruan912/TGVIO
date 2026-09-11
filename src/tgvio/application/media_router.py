from __future__ import annotations

from pathlib import Path

from tgvio.application.ports import MediaDownloader, TransferProgressCallback
from tgvio.domain.job import MediaItem


class RoutedMediaDownloader:
    """Dispatch one MediaItem to a downloader by durable source_type metadata."""

    def __init__(
        self,
        default: MediaDownloader,
        routes: dict[str, MediaDownloader] | None = None,
    ) -> None:
        self._default = default
        self._routes = dict(routes or {})

    async def download(
        self,
        item: MediaItem,
        target_dir: Path,
        progress_callback: TransferProgressCallback | None = None,
    ) -> MediaItem:
        source_type = str(item.metadata.get("source_type") or "telegram").strip().lower()
        downloader = self._routes.get(source_type, self._default)
        return await downloader.download(item, target_dir, progress_callback)
