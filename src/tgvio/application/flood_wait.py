"""Global Telegram rate-limit gate.

Telegram answers an over-eager client with ``FloodWait``, and the only useful
reaction is to stop asking for a while: pushing on through a flood wait burns
the remaining quota and turns one delay into a batch of failed jobs. Waiting for
the owner to notice would be too slow (the owner may be asleep), so the gate
reuses the durable queue pause the scheduler already honours and lifts it by
itself once the window expires.

Nothing here imports Telethon: the application layer must not depend on a
transport, so the wait is recognised from the exception type name and text.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import re
import time

from tgvio.application.ports import JobRepository
from tgvio.domain.control import QueueControlState


_FLOOD_REASON_PREFIX = "flood_wait:"
_FLOOD_CODE_RE = re.compile(r"FLOOD_WAIT_(\d+)", re.IGNORECASE)
_FLOOD_TEXT_RE = re.compile(r"a wait of (\d+) seconds", re.IGNORECASE)
DEFAULT_WAIT_SECONDS = 300
MAX_WAIT_SECONDS = 24 * 60 * 60

_LOG = logging.getLogger("tgvio.flood_wait")


def parse_flood_wait_seconds(exc: object) -> int | None:
    """Return the seconds Telegram asked for, or ``None`` if this is not a flood wait.

    Telethon exposes ``FloodWaitError.seconds`` (and ``.value`` on some paths).
    A wrapped or re-raised error loses that type, so the text forms
    ``FLOOD_WAIT_42`` and ``A wait of 42 seconds is required`` are recognised
    too; a bare flood-wait marker without a number falls back to the default.
    """

    name = type(exc).__name__
    is_flood = "FloodWait" in name or name == "Flood"
    seconds: int | None = None
    if is_flood:
        for attribute in ("seconds", "value"):
            candidate = getattr(exc, attribute, None)
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
                seconds = candidate
                break
    if seconds is None:
        text = str(exc)
        match = _FLOOD_CODE_RE.search(text) or _FLOOD_TEXT_RE.search(text)
        if match is not None:
            seconds = int(match.group(1))
        elif is_flood or "FLOOD_WAIT" in text.upper():
            seconds = DEFAULT_WAIT_SECONDS
        else:
            return None
    return max(1, min(int(seconds), MAX_WAIT_SECONDS))


class FloodWaitGate:
    """Durable "do not publish until T" gate built on the queue pause.

    ``arm`` never shortens an existing window, and ``tick`` lifts the pause only
    while the reason is ours, so an owner pause stays exactly as the owner set it.
    """

    def __init__(
        self,
        repository: JobRepository,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._clock = clock

    @staticmethod
    def pause_reason(until: int) -> str:
        return f"{_FLOOD_REASON_PREFIX}{int(until)}"

    @staticmethod
    def until_from_reason(reason: str | None) -> int | None:
        if not reason or not reason.startswith(_FLOOD_REASON_PREFIX):
            return None
        try:
            return int(reason[len(_FLOOD_REASON_PREFIX):])
        except ValueError:
            return None

    async def arm(self, exc: object) -> int | None:
        """Pause the publish queue for the wait Telegram asked for.

        Returns the epoch second the gate lifts at, or ``None`` when the failure
        was not a flood wait.
        """

        if isinstance(exc, bool):
            return None
        if isinstance(exc, int):
            seconds: int | None = max(1, min(int(exc), MAX_WAIT_SECONDS))
        else:
            seconds = parse_flood_wait_seconds(exc)
        if seconds is None:
            return None
        until = int(self._clock()) + seconds
        state: QueueControlState = await self._repository.get_queue_control()
        current = self.until_from_reason(state.pause_reason)
        if current is not None and current >= until:
            # An existing window already covers this one; report the effective
            # lift time rather than the shorter wait we were just handed.
            return current
        await self._repository.set_queue_paused(True, reason=self.pause_reason(until))
        _LOG.warning(
            "telegram flood wait: publishing paused for %ss (until %s)", seconds, until
        )
        return until

    async def tick(self) -> int | None:
        """Lift an expired flood-wait pause.

        Returns the epoch second the gate lifts at while it is still armed, and
        ``None`` when publishing is free (including a pause that is not ours).
        """

        state: QueueControlState = await self._repository.get_queue_control()
        if not state.paused:
            return None
        until = self.until_from_reason(state.pause_reason)
        if until is None:
            return None
        if int(self._clock()) < until:
            return until
        await self._repository.set_queue_paused(False)
        _LOG.info("telegram flood wait elapsed; publishing resumed")
        return None
