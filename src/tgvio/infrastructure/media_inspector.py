from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from dataclasses import replace
from pathlib import Path

from tgvio.domain.job import MediaItem, MediaKind


TELEGRAM_SOFT_LIMIT_BYTES = 2_000_000_000
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_PLAYABLE_VIDEO_CONTAINERS = {"mp4", "mov"}
_PLAYABLE_VIDEO_CODECS = {"h264", "hevc", "av1", "vp9"}


class FFprobeMediaInspector:
    def __init__(self, *, ffprobe_bin: str = "ffprobe", timeout: float = 20.0) -> None:
        self._ffprobe_bin = ffprobe_bin
        self._timeout = timeout

    async def inspect(self, item: MediaItem) -> MediaItem:
        if item.kind == MediaKind.TEXT:
            return item
        if not item.local_path:
            raise ValueError(f"media item {item.index} has no local_path")
        path = Path(item.local_path)
        if not path.is_file():
            raise FileNotFoundError(path)

        stat = path.stat()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        sha256 = await asyncio.to_thread(self._sha256, path)
        should_probe = self._should_probe(item, mime_type)
        probe = await self._probe(path) if should_probe else {}
        streams = probe.get("streams") or []
        fmt = probe.get("format") or {}
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

        container = self._container_name(fmt.get("format_name"))
        kind = self._infer_kind(path, mime_type, video, audio)
        codec = str((video or audio or {}).get("codec_name") or "") or None
        width = self._as_int((video or {}).get("width"))
        height = self._as_int((video or {}).get("height"))
        duration = self._duration(fmt, video, audio)
        faststart = self._detect_faststart(path) if container in {"mp4", "mov"} else None
        playable = (
            kind == MediaKind.VIDEO
            and container in _PLAYABLE_VIDEO_CONTAINERS
            and codec in _PLAYABLE_VIDEO_CODECS
        )
        metadata = dict(item.metadata)
        metadata.update(
            {
                "has_video_stream": video is not None,
                "has_audio_stream": audio is not None,
                "album_eligible": kind in {MediaKind.PHOTO, MediaKind.VIDEO},
                "telegram_streamable_candidate": playable,
                "faststart": faststart,
                "faststart_candidate": playable and faststart is False,
                "send_as_document_candidate": kind in {MediaKind.DOCUMENT, MediaKind.AUDIO}
                or (kind == MediaKind.VIDEO and not playable),
                "large_file": stat.st_size > TELEGRAM_SOFT_LIMIT_BYTES,
                "probe_skipped": not should_probe,
            }
        )
        return replace(
            item,
            kind=kind,
            name=path.name,
            size_bytes=stat.st_size,
            mime_type=mime_type,
            width=width,
            height=height,
            duration_seconds=duration,
            container=container,
            codec=codec,
            sha256=sha256,
            metadata=metadata,
        )

    async def _probe(self, path: Path) -> dict:
        proc = await asyncio.create_subprocess_exec(
            self._ffprobe_bin,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise TimeoutError(f"ffprobe timed out for item: {path.name}")
        if proc.returncode != 0:
            message = stderr.decode("utf-8", "replace").strip() or "ffprobe failed"
            raise RuntimeError(message)
        return json.loads(stdout.decode("utf-8") or "{}")

    @staticmethod
    def _should_probe(item: MediaItem, mime_type: str) -> bool:
        if item.kind in {MediaKind.PHOTO, MediaKind.VIDEO}:
            return True
        return mime_type.startswith(("image/", "video/", "audio/"))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _container_name(value: object) -> str | None:
        if not value:
            return None
        return str(value).split(",", 1)[0].strip().lower() or None

    @staticmethod
    def _infer_kind(path: Path, mime_type: str, video: dict | None, audio: dict | None) -> MediaKind:
        suffix = path.suffix.lower()
        if suffix in _IMAGE_EXTS or mime_type.startswith("image/"):
            return MediaKind.PHOTO
        if video is not None or mime_type.startswith("video/"):
            return MediaKind.VIDEO
        if audio is not None or mime_type.startswith("audio/"):
            return MediaKind.AUDIO
        return MediaKind.DOCUMENT

    @staticmethod
    def _as_int(value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _duration(fmt: dict, video: dict | None, audio: dict | None) -> float | None:
        for value in (fmt.get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _detect_faststart(path: Path) -> bool | None:
        try:
            with path.open("rb") as handle:
                head = handle.read(4 * 1024 * 1024)
            moov = head.find(b"moov")
            mdat = head.find(b"mdat")
            if moov < 0 and mdat < 0:
                return None
            if moov < 0:
                return False
            if mdat < 0:
                return True
            return moov < mdat
        except OSError:
            return None
