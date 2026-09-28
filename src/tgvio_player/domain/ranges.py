from __future__ import annotations

from dataclasses import dataclass
import re


_SINGLE_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeNotSatisfiable(ValueError):
    """The Player only accepts a valid, satisfiable single byte range."""

    status_code = 416


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def header_value(self) -> str:
        return f"bytes={self.start}-{self.end}"


def parse_single_range(value: str | None, *, size_bytes: int) -> ByteRange | None:
    """Parse ``bytes=N-M``, ``bytes=N-`` and the RFC 7233 suffix form ``bytes=-N``.

    Multipart ranges (``bytes=0-9,20-29``) stay unsupported, like any player
    that only ever asks for one slice at a time. A missing Range means a normal
    full GET, represented by ``None``.
    """
    if value is None:
        return None
    if size_bytes < 0:
        raise ValueError("size_bytes must not be negative")
    match = _SINGLE_RANGE.fullmatch(value.strip())
    if match is None or size_bytes == 0:
        raise RangeNotSatisfiable("invalid or unsupported range")
    start_text, end_text = match.group(1), match.group(2)
    if not start_text and not end_text:
        raise RangeNotSatisfiable("invalid or unsupported range")
    if not start_text:
        # Suffix form: the last N bytes. This is how a player asks for the tail
        # of a non-faststart file, so it must not be refused.
        suffix = int(end_text)
        if suffix <= 0:
            raise RangeNotSatisfiable("invalid suffix range")
        return ByteRange(start=max(0, size_bytes - suffix), end=size_bytes - 1)
    start = int(start_text)
    if start >= size_bytes:
        raise RangeNotSatisfiable("range starts beyond resource")
    end = size_bytes - 1 if not end_text else int(end_text)
    if end < start:
        raise RangeNotSatisfiable("range end precedes start")
    return ByteRange(start=start, end=min(end, size_bytes - 1))
