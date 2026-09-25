from __future__ import annotations

from collections.abc import Callable

from tgvio_player.domain.ranges import ByteRange
from tgvio_player.domain.storage_settings import PlayerStorageSettings
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse


class PlayerMediaReader:
    """Read catalog media, falling back to a verified Player favorite copy."""

    def __init__(
        self,
        repository: object,
        archive_reader: object,
        settings_provider: Callable[[], object],
        credentials_provider: Callable[[PlayerStorageSettings], tuple[str, str]],
        client_factory: Callable[[str, str, str], object],
    ) -> None:
        self._repository = repository
        self._archive_reader = archive_reader
        self._settings_provider = settings_provider
        self._credentials_provider = credentials_provider
        self._client_factory = client_factory

    async def open_range(
        self, package_path: str, remote_relpath: str, byte_range: ByteRange | None
    ) -> WebDavRangeResponse:
        if package_path == "__player_favorite__":
            return await self._open_favorite(remote_relpath, byte_range)
        response = await self._archive_reader.open_range(
            package_path, remote_relpath, byte_range
        )
        if response.status != 404:
            return response
        media_id = await self._repository.favorite_media_id_for_archive_location(
            package_path, remote_relpath
        )
        if media_id is None:
            return response
        location = await self._repository.get_favorite_location(media_id)
        if location is None:
            return response
        return await self._open_favorite(location[0], byte_range)

    async def _open_favorite(
        self, relpath: str, byte_range: ByteRange | None
    ) -> WebDavRangeResponse:
        settings = await self._settings_provider()
        username, password = self._credentials_provider(settings)
        client = self._client_factory(settings.endpoint_url, username, password)
        await client.open()
        response = await client.open_range(
            f"{settings.player_root}/{relpath}", byte_range
        )

        async def body():
            try:
                async for chunk in response.body:
                    yield chunk
            finally:
                await client.close()

        return WebDavRangeResponse(
            response.status, response.content_type, response.content_length,
            response.content_range, response.etag, body(),
        )
