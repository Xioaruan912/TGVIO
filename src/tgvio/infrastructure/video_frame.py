"""Fast single-frame extraction for preview-only video fragments.

The source reader may only have the first few megabytes of a video (embedded
thumbnails can be missing). This helper turns such a fragment into one small
JPEG so the pick grid still shows something. It never raises: an undecodable
fragment simply yields ``None``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from tgvio.observability import log_event


class VideoFrameExtractor:
    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        max_width: int = 320,
        timeout: float = 8.0,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._max_width = max(96, int(max_width))
        self._timeout = max(2.0, float(timeout))
        self._semaphore = asyncio.Semaphore(1)
        self._log = logging.getLogger("tgvio.telegram.preview")

    def build_args(self, source: Path, output: Path) -> list[str]:
        return [
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            f"scale={self._max_width}:-2",
            "-q:v",
            "4",
            "-update",
            "1",
            str(output),
        ]

    async def extract(self, source: Path, output: Path) -> Path | None:
        if not Path(source).is_file():
            return None
        output.parent.mkdir(parents=True, exist_ok=True)
        args = self.build_args(Path(source), Path(output))
        async with self._semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._ffmpeg,
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError:
                return None
            try:
                await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return None
            except asyncio.CancelledError:
                proc.kill()
                await proc.communicate()
                raise
        if int(proc.returncode or 0) != 0 or not Path(output).is_file():
            log_event(
                self._log,
                logging.DEBUG,
                "telegram.preview.frame_unavailable",
                "Video fragment produced no decodable frame",
            )
            return None
        return Path(output)
