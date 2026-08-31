"""DP1 destination profile service."""

from __future__ import annotations

import re
from typing import Any

from ..repository.sqlite import DestinationProfileRecord, RepositoryError, SQLiteRepository


_FOOTER_VAR = re.compile(r"\{([a-z_]+)\}")
_FOOTER_ALLOWED = frozenset({"channel_at", "group_at", "profile_name"})


class DestinationProfileManager:
    def __init__(self, repository: SQLiteRepository, default_profile: DestinationProfileRecord) -> None:
        self.repository = repository
        self._default_profile = default_profile

    @property
    def current_profile(self) -> DestinationProfileRecord:
        return self._default_profile

    def current_snapshot(self) -> dict[str, Any]:
        return self.repository.destination_profile_snapshot(self._default_profile)

    async def refresh_default(self) -> DestinationProfileRecord:
        profile = await self.repository.get_default_destination_profile()
        if profile is None:
            raise RepositoryError("no enabled default destination profile")
        self._default_profile = profile
        return profile

    async def list_profiles(self, *, enabled_only: bool = False) -> list[DestinationProfileRecord]:
        return await self.repository.list_destination_profiles(enabled_only=enabled_only)

    async def create_profile(self, **kwargs: Any) -> DestinationProfileRecord:
        self.validate_footer_template(str(kwargs.get("footer_template") or ""))
        return await self.repository.create_destination_profile(**kwargs)

    async def set_default(self, profile_id: int) -> bool:
        changed = await self.repository.set_default_destination_profile(profile_id)
        if changed:
            await self.refresh_default()
        return changed

    async def mark_verified(self, profile_id: int) -> bool:
        changed = await self.repository.mark_destination_profile_verified(profile_id)
        if changed and self._default_profile.id == int(profile_id):
            await self.refresh_default()
        return changed

    async def disable(self, profile_id: int) -> str:
        return await self.repository.disable_destination_profile(profile_id)

    async def update_profile(self, profile_id: int, **changes: Any) -> str:
        if "footer_template" in changes:
            self.validate_footer_template(str(changes.get("footer_template") or ""))
        result = await self.repository.update_destination_profile(profile_id, **changes)
        if result == "ok" and self._default_profile.id == int(profile_id):
            await self.refresh_default()
        return result

    @staticmethod
    def validate_footer_template(template: str) -> None:
        text = str(template or "")
        if len(text) > 512:
            raise ValueError("footer template is too long")
        for match in _FOOTER_VAR.finditer(text):
            if match.group(1) not in _FOOTER_ALLOWED:
                raise ValueError("footer template contains unsupported variable")
        stripped = _FOOTER_VAR.sub("", text)
        if "{" in stripped or "}" in stripped:
            raise ValueError("footer template contains invalid braces")

    @classmethod
    def render_footer(cls, template: str, profile: DestinationProfileRecord) -> str:
        cls.validate_footer_template(template)
        values = {
            "channel_at": profile.channel_at,
            "group_at": profile.group_at,
            "profile_name": profile.name,
        }
        return _FOOTER_VAR.sub(lambda m: str(values[m.group(1)]), str(template or ""))[:1024]

    @classmethod
    def render_snapshot_footer(cls, snapshot: dict[str, Any]) -> str:
        template = str(snapshot.get("footer_template") or "")
        if not template:
            return " ".join(
                value
                for value in (
                    str(snapshot.get("channel_at") or ""),
                    str(snapshot.get("group_at") or ""),
                )
                if value
            ).strip()
        cls.validate_footer_template(template)
        values = {
            "channel_at": str(snapshot.get("channel_at") or ""),
            "group_at": str(snapshot.get("group_at") or ""),
            "profile_name": str(snapshot.get("name") or ""),
        }
        return _FOOTER_VAR.sub(lambda m: values[m.group(1)], template)[:1024]
