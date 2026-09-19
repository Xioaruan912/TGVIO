"""ffmpeg contact-sheet builder for ``/pick`` previews.

Tiles up to N small thumbnails into one bounded JPEG so the owner can see a whole
page of picks at a glance. It never needs fonts: the grid position is the row
number, so no text is drawn.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Sequence

from tgvio.observability import log_event

_PLACEHOLDER = "0x1f1f1f"
_MAX_COLUMNS = 5
_MIN_QUALITY = 2
_MAX_QUALITY = 12


class ThumbnailGridBuilder:
    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        tile: int = 320,
        columns: int = _MAX_COLUMNS,
        max_bytes: int = 1024 * 1024,
        timeout: float = 60.0,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._tile = max(96, int(tile))
        self._columns = max(1, min(int(columns), _MAX_COLUMNS))
        self._max_bytes = max(64 * 1024, int(max_bytes))
        self._timeout = max(5.0, float(timeout))
        self._semaphore = asyncio.Semaphore(1)
        self._log = logging.getLogger("tgvio.telegram.preview")

    def build_args(
        self,
        slots: Sequence[Path | None],
        output: Path,
        *,
        quality: int = 3,
    ) -> list[str]:
        """Deterministic ffmpeg argv (also used by tests)."""

        count = len([slot for slot in slots])
        if count == 0:
            return []
        columns = min(self._columns, count)
        rows = (count + columns - 1) // columns
        args: list[str] = ["-y", "-hide_banner", "-loglevel", "error"]
        for slot in slots:
            if slot is None:
                args += [
                    "-f",
                    "lavfi",
                    "-t",
                    "0.04",
                    "-i",
                    f"color=c={_PLACEHOLDER}:s={self._tile}x{self._tile}",
                ]
            else:
                args += ["-i", str(slot)]
        filters: list[str] = []
        for index in range(count):
            filters.append(
                f"[{index}:v]scale={self._tile}:{self._tile}"
                ":force_original_aspect_ratio=decrease,"
                f"pad={self._tile}:{self._tile}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1[v{index}]"
            )
        layout = "|".join(
            f"{(index % columns) * self._tile}_{(index // columns) * self._tile}"
            for index in range(count)
        )
        joined = "".join(f"[v{index}]" for index in range(count))
        filters.append(f"{joined}xstack=inputs={count}:layout={layout}[grid]")
        args += [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[grid]",
            "-frames:v",
            "1",
            "-update",
            "1",
            "-q:v",
            str(max(_MIN_QUALITY, min(_MAX_QUALITY, int(quality)))),
            str(output),
        ]
        return args

    def grid_shape(self, count: int) -> tuple[int, int]:
        if count <= 0:
            return (0, 0)
        columns = min(self._columns, count)
        return (columns, (count + columns - 1) // columns)

    async def build(self, slots: Sequence[Path | None], output: Path) -> Path | None:
        """Render the contact sheet; ``None`` when nothing usable was produced."""

        if not slots:
            return None
        output.parent.mkdir(parents=True, exist_ok=True)
        async with self._semaphore:
            for quality in (3, 7, _MAX_QUALITY):
                if await self._run_once(slots, output, quality):
                    try:
                        size = output.stat().st_size
                    except OSError:
                        continue
                    if 0 < size <= self._max_bytes:
                        return output
                output.unlink(missing_ok=True)
        log_event(
            self._log,
            logging.WARNING,
            "telegram.preview.grid_failed",
            "Thumbnail grid could not be produced within budget",
            tile_count=len(slots),
        )
        return None

    async def _run_once(
        self,
        slots: Sequence[Path | None],
        output: Path,
        quality: int,
    ) -> bool:
        args = self.build_args(slots, output, quality=quality)
        if not args:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            log_event(
                self._log,
                logging.WARNING,
                "telegram.preview.ffmpeg_unavailable",
                "ffmpeg could not be started for the pick grid",
                exception_type=type(exc).__name__,
            )
            return False
        try:
            await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return False
        except asyncio.CancelledError:
            proc.kill()
            await proc.communicate()
            raise
        return int(proc.returncode or 0) == 0 and output.is_file()
