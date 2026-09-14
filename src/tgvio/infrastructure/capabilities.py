from __future__ import annotations

import importlib.util
import shutil

from tgvio.domain.diagnostics import CapabilitiesDiagnostic


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def probe_environment_capabilities() -> CapabilitiesDiagnostic:
    """Report which optional/system tools the runtime actually has.

    This is a non-fatal, read-only self-check so environment drift (e.g. a
    missing `hachoir` that degrades Telethon video metadata) becomes visible in
    `/diag` instead of silently producing broken previews.
    """

    return CapabilitiesDiagnostic(
        ffmpeg=shutil.which("ffmpeg") is not None,
        ffprobe=shutil.which("ffprobe") is not None,
        yt_dlp=_module_available("yt_dlp"),
        cryptg=_module_available("cryptg"),
        hachoir=_module_available("hachoir"),
    )
