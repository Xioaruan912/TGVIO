"""S1 source-channel profile facade."""

from __future__ import annotations

import time
from typing import Any

from ..repository.sqlite import SourceProfileRecord, SQLiteRepository


class SourceProfileManager:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    async def list_profiles(self) -> list[SourceProfileRecord]:
        return await self.repository.list_source_profiles()

    async def get(self, profile_id: int) -> SourceProfileRecord | None:
        return await self.repository.get_source_profile(profile_id)

    async def get_enabled_by_peer(self, source_peer_id: int) -> SourceProfileRecord | None:
        return await self.repository.get_enabled_source_profile_by_peer(source_peer_id)

    async def create_verified(
        self,
        *,
        name: str,
        source_peer: str,
        source_peer_id: int,
        destination_profile_id: int,
        owner_user_id: int,
    ) -> SourceProfileRecord:
        return await self.repository.create_source_profile(
            name=name,
            source_peer=source_peer,
            source_peer_id=source_peer_id,
            destination_profile_id=destination_profile_id,
            owner_user_id=owner_user_id,
            verified_at=time.time(),
        )

    async def set_enabled(self, profile_id: int, enabled: bool) -> str:
        return await self.repository.update_source_profile(profile_id, enabled=bool(enabled))

    async def update(self, profile_id: int, **changes: Any) -> str:
        return await self.repository.update_source_profile(profile_id, **changes)

    async def accept_event(
        self,
        *,
        profile: SourceProfileRecord,
        source_message_id: int,
        grouped_id: int | None,
    ):
        return await self.repository.record_source_event(
            source_profile_id=profile.id,
            source_peer_id=profile.source_peer_id,
            source_message_id=int(source_message_id),
            grouped_id=grouped_id,
        )

    async def mark_enqueued(self, event_ids: list[int], seq: int) -> None:
        await self.repository.mark_source_events_enqueued(event_ids, job_legacy_seq=seq)

    async def mark_failed(self, event_ids: list[int], code: str) -> None:
        await self.repository.mark_source_events_failed(event_ids, error_code=code)
