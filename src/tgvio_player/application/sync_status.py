from __future__ import annotations

from dataclasses import dataclass
import time


@dataclass(slots=True)
class SyncStatus:
    """What the last catalog sync saw, for the status page. One writer: the sync loop."""

    last_full_at: float | None = None
    last_error_at: float | None = None
    active_videos: int | None = None
    rejected: int = 0

    def record(self, *, full: bool, active_videos: int, rejected: int) -> None:
        if full:
            self.last_full_at = time.time()
        self.active_videos = active_videos
        self.rejected = rejected

    def failed(self) -> None:
        self.last_error_at = time.time()
