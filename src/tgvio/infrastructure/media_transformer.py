from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


@dataclass(frozen=True, slots=True)
class SplitBundle:
    mode: str
    manifest_path: Path
    parts: tuple[Path, ...]
    original_name: str
    original_sha256: str


class FFmpegMediaTransformer:
    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        timeout: float = 120.0,
        split_timeout: float = 24 * 3600.0,
    ) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin
        self._timeout = timeout
        self._split_timeout = split_timeout

    async def make_video_cover(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
        duration_seconds: float | None,
        max_width: int,
    ) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        output = target_dir / f"cover-{item_index}.jpg"
        seeks = self._cover_seeks(duration_seconds)
        last_error = "no usable frame"
        for seek in seeks:
            result = await self._run(
                self._ffmpeg_bin,
                "-y",
                "-v",
                "error",
                "-ss",
                f"{seek:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale={max_width}:{max_width}:force_original_aspect_ratio=decrease",
                "-q:v",
                "3",
                str(output),
            )
            if result[0] != 0 or not output.is_file() or output.stat().st_size <= 0:
                last_error = result[2] or "ffmpeg produced no frame"
                continue
            if await self._frame_is_blank(output):
                output.unlink(missing_ok=True)
                last_error = "generated frame was blank"
                continue
            return output
        raise RuntimeError(f"unable to generate video cover: {last_error[-500:]}")

    async def make_video_thumbnail(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
        duration_seconds: float | None,
        max_size: int = 320,
        max_bytes: int = 1_000_000,
    ) -> Path | None:
        """Create a Telegram-friendly JPEG thumbnail without blocking publish.

        Automatic selection samples several positions across the video when a
        duration is known. Black and near-white/blank frames are rejected. JPEG
        quality is reduced only as much as needed to stay under the byte limit.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        output = target_dir / f"thumb-{item_index}.jpg"
        if duration_seconds and duration_seconds > 0:
            seeks = tuple(
                max(0.0, duration_seconds * ratio)
                for ratio in (0.05, 0.10, 0.20, 0.30, 0.50, 0.70, 0.80)
            ) + (1.0, 0.0)
        else:
            seeks = (1.0, 0.0)

        for seek in seeks:
            frame_is_blank: bool | None = None
            for quality in (5, 8, 12, 18, 24, 30):
                output.unlink(missing_ok=True)
                code, _stdout, _stderr = await self._run(
                    self._ffmpeg_bin,
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{seek:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={max_size}:{max_size}:force_original_aspect_ratio=decrease",
                    "-q:v",
                    str(quality),
                    str(output),
                )
                if code != 0 or not output.is_file() or output.stat().st_size <= 0:
                    break
                if frame_is_blank is None:
                    frame_is_blank = await self._frame_is_blank(output)
                if frame_is_blank:
                    output.unlink(missing_ok=True)
                    break
                if output.stat().st_size <= max_bytes:
                    return output
            output.unlink(missing_ok=True)
        return None

    async def remux_faststart(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
    ) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix if source.suffix.lower() in {".mp4", ".mov"} else ".mp4"
        output = target_dir / f"faststart-{item_index}{suffix}"
        code, _stdout, stderr = await self._run(
            self._ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        )
        if code != 0 or not output.is_file() or output.stat().st_size <= 0:
            raise RuntimeError(f"faststart remux failed: {(stderr or 'unknown')[-500:]}")
        return output

    async def normalize_thumbnail(
        self,
        source: Path,
        target_dir: Path,
        *,
        max_size: int = 320,
        max_bytes: int = 1_000_000,
    ) -> Path | None:
        """Downscale an owner-supplied image into a Telegram-friendly thumbnail.

        Returns ``None`` when the source cannot be decoded as an image so the
        caller can fall back to the auto-generated frame instead of failing the
        whole publish step.
        """

        target_dir.mkdir(parents=True, exist_ok=True)
        output = target_dir / "custom-thumb.jpg"
        for quality in (5, 8, 12, 18, 24, 30):
            output.unlink(missing_ok=True)
            code, _stdout, _stderr = await self._run(
                self._ffmpeg_bin,
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale={max_size}:{max_size}:force_original_aspect_ratio=decrease",
                "-q:v",
                str(quality),
                str(output),
            )
            if code != 0 or not output.is_file() or output.stat().st_size <= 0:
                return None
            if output.stat().st_size <= max_bytes:
                return output
        output.unlink(missing_ok=True)
        return None

    async def make_binary_volumes(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
        part_bytes: int,
    ) -> SplitBundle:
        return await asyncio.to_thread(
            self._make_binary_volumes_sync,
            source,
            target_dir,
            item_index,
            part_bytes,
        )

    async def make_playable_segments(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
        part_bytes: int,
        duration_seconds: float | None,
    ) -> SplitBundle:
        self._validate_split(source, target_dir, part_bytes)
        target = target_dir / f"split-{item_index}-video"
        target.mkdir(parents=True, exist_ok=True)
        safe_name = self._safe_name(source.name)
        stem = Path(safe_name).stem.replace("%", "_") or f"video-{item_index}"
        duration = duration_seconds or await self._probe_duration(source)
        if duration <= 0:
            raise RuntimeError("video split requires a probeable duration")
        source_size = max(1, source.stat().st_size)
        segment_time = max(0.5, duration * part_bytes / source_size * 0.80)
        pattern = target / f"{stem}.segment%03d.mp4"
        last_error = "unable to create bounded playable segments"

        for transcode in (False, True):
            for _attempt in range(8):
                self._cleanup_matching(target, f"{stem}.segment*.mp4")
                if transcode:
                    codec_args = (
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "20",
                        "-flags",
                        "+cgop",
                        "-force_key_frames",
                        f"expr:gte(t,n_forced*{segment_time:.6f})",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                    )
                else:
                    codec_args = ("-c", "copy")
                code, _stdout, stderr = await self._run(
                    self._ffmpeg_bin,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    *codec_args,
                    "-f",
                    "segment",
                    "-segment_time",
                    f"{segment_time:.6f}",
                    "-reset_timestamps",
                    "1",
                    "-segment_format",
                    "mp4",
                    "-segment_format_options",
                    "movflags=+faststart",
                    str(pattern),
                    timeout=self._split_timeout,
                )
                parts = tuple(sorted(target.glob(f"{stem}.segment*.mp4")))
                valid_sizes = len(parts) >= 2 and all(
                    0 < part.stat().st_size <= part_bytes for part in parts
                )
                if code == 0 and valid_sizes:
                    playable = [await self._is_playable(part) for part in parts]
                    if all(playable):
                        return await asyncio.to_thread(
                            self._manifest_bundle,
                            source,
                            target,
                            safe_name,
                            parts,
                            "playable_video_segments",
                        )
                last_error = stderr or last_error
                largest = max(
                    (part.stat().st_size for part in parts),
                    default=part_bytes * 2,
                )
                ratio = part_bytes / max(1, largest)
                segment_time = max(0.25, segment_time * min(0.75, ratio * 0.85))
        raise RuntimeError(f"unable to create playable video segments: {last_error[-500:]}")

    async def _frame_is_blank(self, path: Path) -> bool:
        if await self._frame_is_black(path):
            return True
        return await self._frame_is_white(path)

    async def _frame_is_black(self, path: Path) -> bool:
        return await self._dominant_frame_ratio(path, negate=False) >= 95

    async def _frame_is_white(self, path: Path) -> bool:
        return await self._dominant_frame_ratio(path, negate=True) >= 95

    async def _dominant_frame_ratio(self, path: Path, *, negate: bool) -> int:
        filter_spec = "negate,blackframe=amount=95:threshold=32" if negate else "blackframe=amount=95:threshold=32"
        code, _stdout, stderr = await self._run(
            self._ffmpeg_bin,
            "-v",
            "info",
            "-i",
            str(path),
            "-vf",
            filter_spec,
            "-f",
            "null",
            "-",
        )
        if code not in {0, 1}:
            return 0
        marker = "pblack:"
        pos = stderr.find(marker)
        if pos < 0:
            return 0
        digits = []
        for char in stderr[pos + len(marker) :]:
            if not char.isdigit():
                break
            digits.append(char)
        return int("".join(digits)) if digits else 0

    async def _probe_duration(self, path: Path) -> float:
        code, stdout, _stderr = await self._run(
            self._ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        )
        if code != 0:
            return 0.0
        try:
            return float(stdout.strip())
        except ValueError:
            return 0.0

    async def _is_playable(self, path: Path) -> bool:
        code, stdout, _stderr = await self._run(
            self._ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        )
        return code == 0 and bool(stdout.strip())

    async def _run(
        self,
        *args: str,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout if timeout is None else timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise TimeoutError(f"media transform timed out: {args[0]}")
        return (
            int(proc.returncode or 0),
            stdout.decode("utf-8", "replace").strip(),
            stderr.decode("utf-8", "replace").strip(),
        )

    def _make_binary_volumes_sync(
        self,
        source: Path,
        target_dir: Path,
        item_index: int,
        part_bytes: int,
    ) -> SplitBundle:
        self._validate_split(source, target_dir, part_bytes)
        target = target_dir / f"split-{item_index}-binary"
        target.mkdir(parents=True, exist_ok=True)
        safe_name = self._safe_name(source.name)
        self._cleanup_matching(target, f"{safe_name}.part*")
        original_digest = hashlib.sha256()
        part_paths: list[Path] = []
        index = 0
        with source.open("rb") as source_file:
            while True:
                first = source_file.read(1)
                if not first:
                    break
                index += 1
                final = target / f"{safe_name}.part{index:03d}"
                temporary = target / f".{final.name}.tmp"
                written = 0
                with temporary.open("wb") as out:
                    out.write(first)
                    original_digest.update(first)
                    written = 1
                    while written < part_bytes:
                        chunk = source_file.read(min(1024 * 1024, part_bytes - written))
                        if not chunk:
                            break
                        out.write(chunk)
                        original_digest.update(chunk)
                        written += len(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, final)
                part_paths.append(final)
        if not part_paths:
            raise ValueError("cannot split empty file")
        return self._manifest_bundle(
            source,
            target,
            safe_name,
            tuple(part_paths),
            "binary_volumes",
            original_sha256=original_digest.hexdigest(),
        )

    def _manifest_bundle(
        self,
        source: Path,
        target: Path,
        safe_name: str,
        parts: tuple[Path, ...],
        mode: str,
        *,
        original_sha256: str | None = None,
    ) -> SplitBundle:
        original_hash = original_sha256 or self._sha256(source)
        part_records = [
            {
                "name": part.name,
                "size_bytes": part.stat().st_size,
                "sha256": self._sha256(part),
            }
            for part in parts
        ]
        manifest = {
            "schema_version": 1,
            "mode": mode,
            "original_name": safe_name,
            "original_size_bytes": source.stat().st_size,
            "original_sha256": original_hash,
            "part_count": len(part_records),
            "parts": part_records,
            "reassemble": (
                f"cat {safe_name}.part* > {safe_name}"
                if mode == "binary_volumes"
                else None
            ),
        }
        manifest_path = target / f"{safe_name}.parts.json"
        temporary = target / f".{manifest_path.name}.tmp"
        with temporary.open("w", encoding="utf-8") as out:
            json.dump(manifest, out, ensure_ascii=False, separators=(",", ":"))
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, manifest_path)
        return SplitBundle(
            mode=mode,
            manifest_path=manifest_path,
            parts=parts,
            original_name=safe_name,
            original_sha256=original_hash,
        )

    @staticmethod
    def _validate_split(source: Path, target_dir: Path, part_bytes: int) -> None:
        if not source.is_file() or part_bytes < 1:
            raise ValueError("invalid split source or part size")
        target_dir.mkdir(parents=True, exist_ok=True)
        required = source.stat().st_size + 16 * 1024 * 1024
        if shutil.disk_usage(target_dir).free < required:
            raise RuntimeError("insufficient free disk space for safe media split")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_name(name: str) -> str:
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
        return (candidate or "media.bin")[:180]

    @staticmethod
    def _cleanup_matching(root: Path, pattern: str) -> None:
        for path in root.glob(pattern):
            if path.is_file():
                path.unlink()

    @staticmethod
    def _cover_seeks(duration_seconds: float | None) -> tuple[float, ...]:
        if duration_seconds and duration_seconds > 0:
            return tuple(
                max(0.0, duration_seconds * ratio)
                for ratio in (0.05, 0.10, 0.20, 0.30, 0.50, 0.70)
            ) + (1.0, 0.0)
        return (1.0, 0.0)
