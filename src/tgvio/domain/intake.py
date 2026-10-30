from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SpoilerMode(StrEnum):
    SOURCE = "source"
    ASK = "ask"
    ALWAYS_SPOILER = "always_spoiler"
    ALWAYS_NORMAL = "always_normal"


@dataclass(frozen=True, slots=True)
class IntakeEventKey:
    source_chat_id: int
    source_message_id: int


@dataclass(frozen=True, slots=True)
class UserPreference:
    owner_id: int
    spoiler_mode: SpoilerMode = SpoilerMode.SOURCE
    quiet_mode: bool = False
    style_json: str | None = None
    thumbnail_path: str | None = None
    caption_template: str | None = None
    ytdlp_preset: str | None = None
    ytdlp_audio_only: bool = False
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class JobDisplayMessage:
    job_id: str
    chat_id: int
    message_id: int
    replacement_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
