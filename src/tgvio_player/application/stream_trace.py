"""Where the time of one media stream goes, for that request's log line only.

A stream request starts a trace; the layers it passes through (slot wait, the
range cache, the archive read) note what they did, and the request log reports
it once the response ends. Only numbers and fixed labels are recorded, never a
path or a name. Nothing here changes playback: with no trace started, every
call is a no-op.
"""

from __future__ import annotations

from contextvars import ContextVar
import time

_CURRENT: ContextVar["StreamTrace | None"] = ContextVar("tgvio_player_stream_trace", default=None)


class StreamTrace:
    def __init__(self) -> None:
        self._started = time.monotonic()
        self.fields: dict[str, object] = {}
        # The archive route of the latest read opened under this request.
        self.via: str | None = None

    def elapsed_ms(self) -> float:
        return round((time.monotonic() - self._started) * 1000, 1)

    def mark(self, name: str) -> None:
        """Record when something first happened, in ms since the request began."""
        self.fields.setdefault(name, self.elapsed_ms())

    def note(self, name: str, value: object) -> None:
        """Record a fact once; the first answer is the one the viewer waited on."""
        self.fields.setdefault(name, value)

    def add(self, name: str, amount: int = 1) -> None:
        self.fields[name] = int(self.fields.get(name, 0)) + amount  # type: ignore[arg-type]


def begin() -> tuple[StreamTrace, object]:
    trace = StreamTrace()
    return trace, _CURRENT.set(trace)


def end(token: object) -> None:
    _CURRENT.reset(token)  # type: ignore[arg-type]


def current() -> StreamTrace | None:
    return _CURRENT.get()
