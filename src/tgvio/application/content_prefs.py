"""Owner-scoped content preferences: custom thumbnail, caption template, yt-dlp.

The service validates user input once, persists it through the repository port
and produces the immutable per-job snapshot the downloader/orchestrator read.
"""

from __future__ import annotations

from typing import Any

from tgvio.domain.content import (
    ContentPreferenceError,
    normalize_ytdlp_preset,
    validate_caption_template,
)
from tgvio.domain.intake import UserPreference


def content_policy_snapshot(preference: UserPreference) -> dict[str, Any]:
    """Project the durable preference into the per-job policy fields."""

    snapshot: dict[str, Any] = {}
    if preference.thumbnail_path:
        snapshot["thumbnail_path"] = str(preference.thumbnail_path)
    if preference.caption_template:
        try:
            snapshot["caption_template"] = validate_caption_template(
                preference.caption_template
            )
        except ContentPreferenceError:
            pass
    try:
        preset = normalize_ytdlp_preset(preference.ytdlp_preset)
    except ContentPreferenceError:
        preset = normalize_ytdlp_preset(None)
    snapshot["ytdlp"] = {
        "preset": preset,
        "audio_only": bool(preference.ytdlp_audio_only),
    }
    return snapshot
