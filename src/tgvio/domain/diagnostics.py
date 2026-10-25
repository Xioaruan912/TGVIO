from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class StaticProxyState(StrEnum):
    DISABLED = "disabled"
    CONFIGURED_UNCHECKED = "configured_unchecked"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class CapabilitiesDiagnostic:
    """Non-sensitive environment capability booleans (no paths or versions)."""

    ffmpeg: bool
    ffprobe: bool
    yt_dlp: bool
    cryptg: bool
    hachoir: bool

