"""Reuse source-bound unindexed outputs, with shared transfer and decode budgets."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
import re
import time

from tgvio.adapters.rendition_discovery import RenditionDiscovery
from tgvio.domain.renditions import RenditionTask, safe_path
from tgvio.infrastructure.rendition_encoder import verify_recovered


async def recover(port, task: RenditionTask, heights: tuple[int, ...],
                  work: Path, max_bytes: int) -> dict[int, dict]:
    duration = task.media.get("duration_seconds")
    original_height = task.media.get("height")
    if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
            or not math.isfinite(duration) or not 0 < duration <= 24 * 3600
            or type(original_height) is not int or max_bytes <= 0):
        return {}
    parent = task.media["sha256"]
    root = safe_path(task.root)
    files = await RenditionDiscovery(port, root).files(f"{root}/renditions")
    deadline = time.monotonic() + 240
    remaining = min(max_bytes, 128 * 1024**2)
    result = {}
    for height in heights:
        if height not in (480, 720) or original_height <= height:
            continue
        expression = re.compile(rf"{parent[:16]}-{height}p-([0-9a-f]{{16}})\.mp4")
        candidates = [(name, size, match.group(1)) for name, size in files
                      if (match := expression.fullmatch(name))
                      and 0 < size <= min(remaining, 64 * 1024**2)]
        # At most two candidates per height; never scan/decode the full folder.
        for name, size, suffix in candidates[:2]:
            if size > remaining or time.monotonic() >= deadline:
                break
            remaining -= size
            target = work / f"recovery-{height}.mp4"
            try:
                digest = await port.read_candidate(f"{root}/renditions/{name}", target, size, deadline)
                if not digest.startswith(suffix):
                    continue
                try:
                    spec = await asyncio.wait_for(
                        verify_recovered(target, parent, height, float(duration), digest),
                        max(.001, deadline - time.monotonic()))
                except (ValueError, RuntimeError, TimeoutError):
                    # Invalid/undecodable candidates cannot enter the index.
                    continue
                result[height] = {**spec, "path": f"renditions/{name}"}
                break
            finally:
                target.unlink(missing_ok=True)
    return result
