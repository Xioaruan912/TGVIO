"""Pure helpers for owner-scoped publishing content preferences.

This module is transport independent: it validates and renders the caption
template, models the yt-dlp presets, and never touches Telegram, SQLite or the
filesystem. Keeping it in ``domain`` lets the orchestrator, the repository and
the Bot UI share exactly one source of truth for these rules.
"""

from __future__ import annotations

import re

# yt-dlp quality presets. ``audio`` is a separate mode (see AUDIO_ONLY_PRESET)
# because it changes the produced media kind rather than the video ceiling.
YTDLP_PRESETS: dict[str, str] = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
}
AUDIO_ONLY_PRESET = "audio"
DEFAULT_YTDLP_PRESET = "best"
YTDLP_PRESET_LABELS: dict[str, str] = {
    "best": "最佳画质",
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    AUDIO_ONLY_PRESET: "仅音频",
}
YTDLP_AUDIO_FORMAT = "mp3"

MAX_CAPTION_TEMPLATE_CHARS = 400
MAX_BUTTON_LABEL_CHARS = 64
MAX_BUTTON_URL_CHARS = 512

# Variables the owner may reference in the caption template. The orchestrator
# supplies these exact keys when rendering; anything else is rejected up front
# so a typo cannot silently disappear at publish time.
TEMPLATE_VARIABLES: tuple[str, ...] = (
    "channel",
    "group",
    "date",
    "time",
    "index",
    "total",
    "count",
    "name",
    "kind",
    "part",
    "parts",
)

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_BUTTON_LINE_RE = re.compile(
    r"^\s*button\s*:\s*(?P<label>[^|]+?)\s*\|\s*(?P<url>\S+)\s*$",
    re.IGNORECASE,
)


class ContentPreferenceError(ValueError):
    """Raised when a content preference value is invalid."""


def normalize_ytdlp_preset(value: str | None) -> str:
    key = (value or DEFAULT_YTDLP_PRESET).strip().lower()
    if key in YTDLP_PRESETS or key == AUDIO_ONLY_PRESET:
        return key
    raise ContentPreferenceError(f"unknown yt-dlp preset: {key}")


def ytdlp_format_for(preset: str) -> str:
    normalized = normalize_ytdlp_preset(preset)
    if normalized == AUDIO_ONLY_PRESET:
        return "bestaudio/best"
    return YTDLP_PRESETS[normalized]


def validate_caption_template(value: str | None) -> str:
    text = (value or "").strip()
    if len(text) > MAX_CAPTION_TEMPLATE_CHARS:
        raise ContentPreferenceError(
            f"caption template must be at most {MAX_CAPTION_TEMPLATE_CHARS} characters"
        )
    unknown = sorted(set(_PLACEHOLDER_RE.findall(text)) - set(TEMPLATE_VARIABLES))
    if unknown:
        raise ContentPreferenceError(
            "unknown template variables: " + ", ".join(unknown)
        )
    return text


def render_caption_template(template: str, variables: dict[str, str]) -> str:
    """Render known ``{variable}`` placeholders; unknown keys render empty."""

    def _replace(match: re.Match[str]) -> str:
        return str(variables.get(match.group(1), "") or "")

    return _PLACEHOLDER_RE.sub(_replace, template or "").strip()


def split_template_buttons(template: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Extract ``button: label | https://url`` lines from a rendered template.

    Returns the text with the button lines removed plus the parsed buttons.
    Invalid button lines raise so the owner learns immediately instead of the
    button silently disappearing during publish.
    """

    text_lines: list[str] = []
    buttons: list[tuple[str, str]] = []
    for line in (template or "").splitlines():
        match = _BUTTON_LINE_RE.match(line)
        if match is None:
            text_lines.append(line)
            continue
        label = match.group("label").strip()
        url = match.group("url").strip()
        if not label:
            raise ContentPreferenceError("button label must not be empty")
        if len(label) > MAX_BUTTON_LABEL_CHARS:
            raise ContentPreferenceError("button label is too long")
        if len(url) > MAX_BUTTON_URL_CHARS:
            raise ContentPreferenceError("button URL is too long")
        if not url.lower().startswith(("http://", "https://")):
            raise ContentPreferenceError("button URL must start with http:// or https://")
        buttons.append((label, url))
    return "\n".join(text_lines).strip(), tuple(buttons)
