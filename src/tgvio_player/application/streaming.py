from __future__ import annotations

from dataclasses import dataclass

from tgvio_player.domain.ranges import ByteRange, parse_single_range


@dataclass(frozen=True, slots=True)
class StreamRequest:
    byte_range: ByteRange | None
    status: int
    content_length: int
    content_range: str | None


def prepare_stream_request(range_header: str | None, *, size_bytes: int) -> StreamRequest:
    """Normalize browser input before it reaches the read-only WebDAV adapter."""
    byte_range = parse_single_range(range_header, size_bytes=size_bytes)
    if byte_range is None:
        return StreamRequest(None, 200, size_bytes, None)
    return StreamRequest(
        byte_range=byte_range,
        status=206,
        content_length=byte_range.length,
        content_range=f"bytes {byte_range.start}-{byte_range.end}/{size_bytes}",
    )
