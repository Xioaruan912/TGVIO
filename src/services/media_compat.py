"""M1 media analysis and optional lossless faststart remux service."""

from __future__ import annotations

import logging
import os
from typing import Any

from ..video import MediaMetadata, is_video_path, probe_media_metadata, remux_faststart


logger = logging.getLogger(__name__)


class MediaCompatibilityManager:
    def __init__(
        self,
        *,
        mode: str = "analyze",
        faststart_max_bytes: int = 0,
        transcode_enabled: bool = False,
        disk: Any | None = None,
    ) -> None:
        self.mode = mode if mode in {"off", "analyze", "remux"} else "analyze"
        self.faststart_max_bytes = max(0, int(faststart_max_bytes))
        self.transcode_enabled = bool(transcode_enabled)
        self.disk = disk
        if self.transcode_enabled:
            logger.warning(
                "TRANSCODE_ENABLED is set, but M1 intentionally does not perform automatic lossy transcoding"
            )

    async def process(self, job: Any, paths: str | list[str]):
        if self.mode == "off":
            return None
        values = list(paths) if isinstance(paths, list) else [paths]
        output: list[str] = []
        metadata: dict[str, MediaMetadata] = {}
        notes: list[str] = []
        for path in values:
            if not path or not os.path.isfile(path) or not is_video_path(path):
                output.append(path)
                continue
            final_path = path
            try:
                info = await probe_media_metadata(path)
            except Exception as exc:
                logger.warning("Job #%s M1 ffprobe skipped: %s", job.seq, exc.__class__.__name__)
                notes.append("⚠️ 媒体兼容性未知")
                output.append(path)
                continue

            if info.telegram_streaming_ready:
                notes.append("✅ 可流式播放")
            elif info.needs_faststart and info.video_codec == "h264" and info.audio_codec in (None, "aac"):
                if self.mode == "remux":
                    final_path, info, note = await self._maybe_remux(job, path, info)
                    notes.append(note)
                else:
                    notes.append("🧩 可执行 faststart")
            elif info.video_codec not in (None, "h264") or info.audio_codec not in (None, "aac"):
                notes.append("⚠️ 非 H.264/AAC，按现有文件方式发布")
            else:
                notes.append("⚠️ 不适合渐进播放")

            metadata[os.path.realpath(final_path)] = info
            output.append(final_path)

        job._media_metadata = metadata
        job._media_compat_notes = list(dict.fromkeys(notes))
        if isinstance(paths, list):
            return output
        return output[0] if output else paths

    async def _maybe_remux(
        self,
        job: Any,
        path: str,
        info: MediaMetadata,
    ) -> tuple[str, MediaMetadata, str]:
        size = os.path.getsize(path)
        if self.faststart_max_bytes and size > self.faststart_max_bytes:
            return path, info, "⚠️ faststart 超过大小策略，已跳过"

        previous = self.disk.reserved_bytes(job.seq) if self.disk is not None else 0
        if self.disk is not None:
            decision = self.disk.reserve(job.seq, previous + size)
            if not decision.healthy:
                self._restore_reservation(job.seq, previous)
                return path, info, "⚠️ 磁盘余量不足，已跳过 faststart"
        try:
            remuxed = await remux_faststart(path)
            verified = await probe_media_metadata(remuxed)
            return remuxed, verified, "🧩 已完成 faststart（无损 remux）"
        except Exception as exc:
            logger.warning("Job #%s M1 faststart fallback: %s", job.seq, exc.__class__.__name__)
            return path, info, "⚠️ faststart 失败，继续使用原文件"
        finally:
            if self.disk is not None:
                self._restore_reservation(job.seq, previous)

    def _restore_reservation(self, seq: int, previous: int) -> None:
        if previous > 0:
            self.disk.reserve(seq, previous)
        else:
            self.disk.release(seq)

