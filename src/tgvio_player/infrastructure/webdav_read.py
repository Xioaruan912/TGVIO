from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from tgvio_player.domain.catalog import safe_remote_path
from tgvio_player.domain.ranges import ByteRange


@dataclass(frozen=True, slots=True)
class WebDavRangeResponse:
    status: int
    content_type: str | None
    content_length: int | None
    content_range: str | None
    etag: str | None
    body: AsyncIterator[bytes]


class WebDavReadClient(Protocol):
    """Read-only transport contract. No write verbs exist in this interface."""

    async def open_range(
        self, remote_path: str, byte_range: ByteRange | None
    ) -> WebDavRangeResponse: ...


class ReadOnlyWebDavAdapter:
    """Validates catalog-owned paths before delegating a streaming Range GET."""

    def __init__(self, client: WebDavReadClient) -> None:
        self._client = client

    async def open_range(
        self, package_path: str, remote_relpath: str, byte_range: ByteRange | None
    ) -> WebDavRangeResponse:
        package = safe_remote_path(package_path, relative=False)
        relpath = safe_remote_path(remote_relpath, relative=True)
        return await self._client.open_range(f"{package}/{relpath}", byte_range)
