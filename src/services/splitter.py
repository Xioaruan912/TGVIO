"""Streaming, verifiable file volumes for Telegram's per-file upload limit."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..security import sanitize_filename


class SplitStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class SplitBundle:
    manifest_path: str
    parts: tuple[str, ...]
    original_name: str
    original_sha256: str
    mode: str = "binary_volumes"


_VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".ts"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("video split requires a probeable duration") from exc
    if result.returncode != 0 or duration <= 0:
        raise RuntimeError("video split requires a playable input")
    return duration


def _playable(path: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _video_segments(source: Path, target: Path, safe_name: str, part_bytes: int) -> tuple[Path, ...]:
    duration = _probe_duration(source)
    segment_time = max(0.5, duration * part_bytes / max(1, source.stat().st_size) * 0.82)
    stem = Path(safe_name).stem.replace("%", "_") or "video"
    pattern = target / f"{stem}.segment%03d.mp4"
    for transcode in (False, True):
        for _attempt in range(8):
            for old in target.glob(f"{stem}.segment*.mp4"):
                old.unlink()
            codec = ["-c", "copy"] if not transcode else [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-flags", "+cgop", "-force_key_frames", f"expr:gte(t,n_forced*{segment_time:.6f})",
                "-c:a", "aac", "-b:a", "192k",
            ]
            result = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", *codec,
                 "-f", "segment", "-segment_time", f"{segment_time:.6f}", "-reset_timestamps", "1",
                 "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart", str(pattern)],
                capture_output=True, text=True, timeout=24 * 3600, check=False,
            )
            parts = tuple(sorted(target.glob(f"{stem}.segment*.mp4")))
            if result.returncode == 0 and len(parts) >= 2 and all(
                0 < part.stat().st_size <= part_bytes and _playable(part) for part in parts
            ):
                return parts
            largest = max((part.stat().st_size for part in parts), default=part_bytes * 2)
            segment_time = max(0.25, segment_time * min(0.75, part_bytes / max(1, largest) * 0.85))
    raise RuntimeError("unable to create independently playable segments below upload limit")


def _manifest_bundle(source_path: Path, target: Path, safe_name: str, paths: tuple[Path, ...], mode: str) -> SplitBundle:
    original_hash = _sha256(source_path)
    parts = [{"name": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)} for path in paths]
    manifest = {
        "schema_version": 2, "mode": mode, "original_name": safe_name,
        "original_size_bytes": source_path.stat().st_size, "original_sha256": original_hash,
        "part_count": len(parts), "parts": parts,
        "reassemble": f"cat {safe_name}.part* > {safe_name}" if mode == "binary_volumes" else None,
    }
    manifest_path = target / f"{safe_name}.parts.json"
    temporary_manifest = target / f".{manifest_path.name}.tmp"
    with temporary_manifest.open("w", encoding="utf-8") as out:
        json.dump(manifest, out, ensure_ascii=False, separators=(",", ":")); out.write("\n"); out.flush(); os.fsync(out.fileno())
    os.replace(temporary_manifest, manifest_path)
    return SplitBundle(str(manifest_path), tuple(str(path) for path in paths), safe_name, original_hash, mode)


def create_split_bundle(source: str, workdir: str, part_bytes: int) -> SplitBundle:
    """Copy *source* into bounded volumes and an atomically-written manifest.

    Parts are intentionally generic documents, not fake video clips: every byte
    is preserved and the manifest gives the recipient an independent checksum.
    """
    source_path = Path(source).resolve()
    root = Path(workdir).resolve()
    if not source_path.is_file() or part_bytes < 1:
        raise ValueError("invalid split source or part size")
    root.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(source_path.name) or "media.bin"
    target = (root / f"split-{hashlib.sha256(str(source_path).encode()).hexdigest()[:12]}").resolve()
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise ValueError("split output escapes managed workdir")
    target.mkdir(exist_ok=True)
    size = source_path.stat().st_size
    free = os.statvfs(target).f_bavail * os.statvfs(target).f_frsize
    if free < size + 16 * 1024 * 1024:
        raise SplitStorageError("insufficient free disk space for safe split volumes")

    if source_path.suffix.lower() in _VIDEO_SUFFIXES:
        return _manifest_bundle(
            source_path, target, safe_name,
            _video_segments(source_path, target, safe_name, part_bytes),
            "playable_video_segments",
        )

    digest = hashlib.sha256()
    parts: list[dict[str, object]] = []
    paths: list[str] = []
    index = 0
    with source_path.open("rb") as source_file:
        while True:
            first = source_file.read(1)
            if not first:
                break
            index += 1
            filename = f"{safe_name}.part{index:03d}"
            final = target / filename
            temporary = target / f".{filename}.tmp"
            part_hash = hashlib.sha256()
            written = 0
            with temporary.open("wb") as out:
                out.write(first)
                digest.update(first)
                part_hash.update(first)
                written = 1
                while written < part_bytes:
                    chunk = source_file.read(min(1024 * 1024, part_bytes - written))
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    part_hash.update(chunk)
                    written += len(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, final)
            paths.append(str(final))
            parts.append({"name": filename, "size_bytes": written, "sha256": part_hash.hexdigest()})
    if not paths:
        raise ValueError("cannot split empty file")
    return _manifest_bundle(source_path, target, safe_name, tuple(Path(path) for path in paths), "binary_volumes")
