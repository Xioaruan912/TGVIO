from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
from typing import Final
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

from aiohttp import BasicAuth, ClientSession, ClientTimeout

from tgvio_player.domain.catalog import safe_remote_path
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.infrastructure.webdav_catalog import WebDavCollectionEntry
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse


_DAV: Final = "DAV:"
_PROPFIND_BODY: Final = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getetag/></d:prop></d:propfind>"""


@dataclass(frozen=True, slots=True)
class WebDavClientSettings:
    base_url: str
    username: str
    password: str
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 30.0


class AioHttpReadOnlyWebDavClient:
    """Bounded Player WebDAV transport with opt-in single-file deletion."""

    def __init__(self, settings: WebDavClientSettings) -> None:
        parsed = urlsplit(settings.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Player WebDAV URL must be an HTTPS URL without userinfo")
        if min(settings.connect_timeout_seconds, settings.read_timeout_seconds) <= 0:
            raise ValueError("WebDAV timeouts must be positive")
        self._base_url = settings.base_url.rstrip("/")
        self._auth = BasicAuth(settings.username, settings.password)
        self._timeout = ClientTimeout(
            total=None,
            connect=settings.connect_timeout_seconds,
            sock_read=settings.read_timeout_seconds,
        )
        self._session: ClientSession | None = None

    async def open(self) -> None:
        if self._session is None:
            self._session = ClientSession(auth=self._auth, timeout=self._timeout)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def list_collection(self, remote_path: str) -> tuple[WebDavCollectionEntry, ...]:
        response = await self._request(
            "PROPFIND",
            remote_path,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            data=_PROPFIND_BODY,
        )
        try:
            if response.status != 207:
                raise RuntimeError(f"WebDAV collection request failed ({response.status})")
            payload = await self._read_bounded(response.content.iter_chunked(64 * 1024), 1024 * 1024)
        finally:
            response.release()
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise RuntimeError("WebDAV collection response is invalid XML") from exc
        parent = safe_remote_path(remote_path, relative=False)
        entries: list[WebDavCollectionEntry] = []
        for item in root.findall(f"{{{_DAV}}}response"):
            href = item.findtext(f"{{{_DAV}}}href")
            name = self._entry_name(href, parent)
            if name is None:
                continue
            resource_type = item.find(f".//{{{_DAV}}}resourcetype")
            is_collection = resource_type is not None and resource_type.find(f"{{{_DAV}}}collection") is not None
            etag = item.findtext(f".//{{{_DAV}}}getetag")
            entries.append(WebDavCollectionEntry(name, is_collection, etag or None))
        return tuple(entries)

    async def get_json(self, remote_path: str, *, max_bytes: int) -> object | None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        response = await self._request("GET", remote_path)
        try:
            if response.status == 404:
                return None
            if not 200 <= response.status < 300:
                raise RuntimeError(f"WebDAV metadata request failed ({response.status})")
            payload = await self._read_bounded(response.content.iter_chunked(64 * 1024), max_bytes)
        finally:
            response.release()
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("WebDAV metadata is not valid JSON") from exc

    async def open_range(self, remote_path: str, byte_range: ByteRange | None) -> WebDavRangeResponse:
        headers = {} if byte_range is None else {"Range": f"bytes={byte_range.start}-{byte_range.end}"}
        response = await self._request("GET", remote_path, headers=headers)

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    yield chunk
            finally:
                response.release()

        content_length = response.headers.get("Content-Length")
        try:
            length = int(content_length) if content_length is not None else None
        except ValueError:
            length = None
        return WebDavRangeResponse(
            response.status,
            response.headers.get("Content-Type"),
            length,
            response.headers.get("Content-Range"),
            response.headers.get("ETag"),
            body(),
        )

    async def delete(self, remote_path: str) -> bool:
        response = await self._request("DELETE", remote_path)
        try:
            if response.status in {200, 202, 204, 404}:
                return True
            return False
        finally:
            response.release()

    async def _request(self, method: str, remote_path: str, **kwargs: object):
        await self.open()
        assert self._session is not None
        return await self._session.request(method, self._url(remote_path), **kwargs)

    def _url(self, remote_path: str) -> str:
        path = safe_remote_path(remote_path, relative=False)
        return f"{self._base_url}/{quote(path, safe='/')}"

    @staticmethod
    async def _read_bounded(chunks: AsyncIterator[bytes], max_bytes: int) -> bytes:
        payload = bytearray()
        async for chunk in chunks:
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise RuntimeError("WebDAV metadata exceeds Player limit")
        return bytes(payload)

    @staticmethod
    def _entry_name(href: str | None, parent: str) -> str | None:
        if not href:
            return None
        path = urlsplit(href).path
        encoded_parent = quote(parent, safe="/")
        marker = f"/{encoded_parent.strip('/')}/"
        if marker not in path:
            return None
        tail = unquote(path.split(marker, 1)[1].strip("/"))
        if not tail or "/" in tail:
            return None
        try:
            return safe_remote_path(tail, relative=True)
        except ValueError:
            return None
