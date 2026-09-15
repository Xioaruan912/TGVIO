"""Owner-scoped content preferences: custom thumbnail, caption template, yt-dlp.

The service validates user input once, persists it through the repository port
and produces the immutable per-job snapshot the downloader/orchestrator read.
"""

from __future__ import annotations

from typing import Any

from tgvio.application.ports import JobRepository
from tgvio.domain.content import (
    ContentPreferenceError,
    normalize_ytdlp_preset,
    validate_caption_template,
)
from tgvio.domain.intake import UserPreference


class ContentPreferenceService:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    async def current(self, owner_id: int) -> UserPreference:
        return await self._repository.get_user_preference(int(owner_id))

    async def set_thumbnail(self, owner_id: int, thumbnail_path: str) -> UserPreference:
        path = (thumbnail_path or "").strip()
        if not path:
            raise ContentPreferenceError("thumbnail path must not be empty")
        return await self._repository.set_user_thumbnail_path(int(owner_id), path)

    async def clear_thumbnail(self, owner_id: int) -> UserPreference:
        return await self._repository.set_user_thumbnail_path(int(owner_id), None)

    async def set_caption_template(self, owner_id: int, template: str) -> UserPreference:
        validated = validate_caption_template(template)
        if not validated:
            return await self._repository.set_user_caption_template(int(owner_id), None)
        return await self._repository.set_user_caption_template(int(owner_id), validated)

    async def clear_caption_template(self, owner_id: int) -> UserPreference:
        return await self._repository.set_user_caption_template(int(owner_id), None)

    async def set_ytdlp(
        self,
        owner_id: int,
        *,
        preset: str | None = None,
        audio_only: bool | None = None,
    ) -> UserPreference:
        current = await self._repository.get_user_preference(int(owner_id))
        resolved_preset = normalize_ytdlp_preset(
            preset if preset is not None else current.ytdlp_preset
        )
        resolved_audio = (
            bool(audio_only) if audio_only is not None else bool(current.ytdlp_audio_only)
        )
        return await self._repository.set_user_ytdlp_options(
            int(owner_id),
            preset=resolved_preset,
            audio_only=resolved_audio,
        )


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
