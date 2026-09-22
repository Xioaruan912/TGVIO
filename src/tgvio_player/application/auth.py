from __future__ import annotations

import time
from collections.abc import Callable

from tgvio_player.domain.auth import (
    SessionCookie,
    new_session_token,
    token_digest,
    verify_access_secret,
)


class SessionService:
    """Owns opaque Player sessions; raw tokens are never stored in SQLite."""

    def __init__(
        self,
        repository: object,
        *,
        access_secret: str,
        cookie_name: str = "tgvio_player_session",
        ttl_seconds: int = 8 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._access_secret = access_secret
        self._cookie_name = cookie_name
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    async def login(self, supplied_secret: str | None) -> SessionCookie | None:
        if not verify_access_secret(self._access_secret, supplied_secret):
            return None
        token = new_session_token()
        await self._repository.create_player_session(
            token_digest(token), expires_at=int(self._clock()) + self._ttl_seconds
        )
        return SessionCookie(self._cookie_name, token, self._ttl_seconds)

    async def authenticate(self, cookie_token: str | None) -> bool:
        if not cookie_token:
            return False
        return await self._repository.has_player_session(
            token_digest(cookie_token), now=int(self._clock())
        )

    async def logout(self, cookie_token: str | None) -> None:
        if cookie_token:
            await self._repository.delete_player_session(token_digest(cookie_token))
