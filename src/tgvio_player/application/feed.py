from __future__ import annotations

import random

from tgvio_player.domain.feed import apply_recent_exclusion, shuffled_cycle


class ShuffleDeckService:
    """Persistent uniform shuffle cycles, independent of favorites or watch time."""

    def __init__(
        self,
        repository: object,
        *,
        rng: random.Random | None = None,
        recent_exclusion: int = 20,
        max_duration_seconds: float | None = None,
    ) -> None:
        self._repository = repository
        self._rng = rng or random.Random()
        self._recent_exclusion = max(0, recent_exclusion)
        self._max_duration_seconds = max_duration_seconds

    async def next_items(self, session_digest: str, *, limit: int) -> list[str]:
        if limit < 1:
            return []
        await self._ensure_deck(session_digest)
        return await self._repository.consume_feed_items(session_digest, limit=limit)

    async def _ensure_deck(self, session_digest: str) -> None:
        if await self._repository.has_unconsumed_feed_items(session_digest):
            return
        if self._max_duration_seconds is not None:
            eligible = await self._repository.list_video_ids(
                max_seconds=self._max_duration_seconds, limit=100_000
            )
        else:
            eligible = await self._repository.list_active_video_ids(limit=100_000)
        if not eligible:
            return
        cycle = await self._repository.next_feed_cycle(session_digest)
        recent = await self._repository.recent_feed_media(
            session_digest, limit=self._recent_exclusion
        )
        deck = apply_recent_exclusion(
            shuffled_cycle(eligible, rng=self._rng),
            recent=recent,
            target=self._recent_exclusion,
        )
        await self._repository.write_feed_cycle(session_digest, cycle=cycle, media_ids=deck)

    async def favorite(self, session_digest: str, media_id: str) -> None:
        await self._repository.set_favorite(session_digest, media_id, enabled=True)

    async def unfavorite(self, session_digest: str, media_id: str) -> None:
        await self._repository.set_favorite(session_digest, media_id, enabled=False)

    async def list_favorites(self, session_digest: str, *, limit: int = 200) -> list[str]:
        return await self._repository.list_favorite_ids(session_digest, limit=limit)
