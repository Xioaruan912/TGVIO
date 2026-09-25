from __future__ import annotations

from collections.abc import AsyncIterator
import ipaddress
import re
from urllib.parse import quote, urlsplit

from aiohttp import BasicAuth, ClientSession, ClientTimeout, TCPConnector

from tgvio_player.application.ports import DeleteReceipt, RemoteFileStat, UploadReceipt
from tgvio_player.infrastructure.webdav_aiohttp import PublicOnlyResolver


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


def validate_webdav_endpoint(value: str) -> str:
    if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
        raise ValueError("WebDAV endpoint must be a public HTTPS origin")
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("WebDAV endpoint has an invalid port") from exc
    if (
        parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username
        or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("WebDAV endpoint must be a public HTTPS origin without userinfo")
    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise ValueError("WebDAV endpoint must use a public hostname")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("WebDAV endpoint must use a public IP address")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("WebDAV endpoint has an invalid port")
    authority = hostname if port is None else f"{hostname}:{port}"
    if address is not None and address.version == 6:
        authority = f"[{hostname}]" if port is None else f"[{hostname}]:{port}"
    return f"https://{authority}"


def safe_storage_relpath(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ValueError("unsafe WebDAV relative path")
    if "\\" in value or _CONTROL_RE.search(value):
        raise ValueError("unsafe WebDAV relative path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("unsafe WebDAV relative path")
    return value


class WebDavWriteError(RuntimeError):
    def __init__(self, operation: str, category: str, status_code: int | None = None) -> None:
        self.operation = operation
        self.category = category
        self.status_code = status_code
        suffix = f" ({status_code})" if status_code is not None else ""
        super().__init__(f"WebDAV {operation} failed: {category}{suffix}")


class AioHttpWebDavWriteClient:
    """A validated WebDAV write client with streamed request bodies."""

    def __init__(
        self,
        endpoint_url: str,
        username: str,
        password: str,
        *,
        session: object | None = None,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = validate_webdav_endpoint(endpoint_url)
        if min(connect_timeout_seconds, read_timeout_seconds) <= 0:
            raise ValueError("WebDAV timeouts must be positive")
        self._auth = BasicAuth(username, password)
        self._timeout = ClientTimeout(
            total=None, connect=connect_timeout_seconds, sock_read=read_timeout_seconds,
        )
        self._session = session
        self._owns_session = session is None

    async def open(self) -> None:
        if self._session is None:
            connector = TCPConnector(
                resolver=PublicOnlyResolver(), use_dns_cache=False, ttl_dns_cache=0,
            )
            self._session = ClientSession(
                auth=self._auth, timeout=self._timeout, connector=connector,
            )

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()  # type: ignore[attr-defined]
        self._session = None

    async def ensure_directory(self, path: str) -> None:
        safe_path = safe_storage_relpath(path)
        current: list[str] = []
        for segment in safe_path.split("/"):
            current.append(segment)
            response = await self._request("MKCOL", "/".join(current), operation="mkdir")
            try:
                self._check(response.status, {201, 405}, "mkdir")
            finally:
                response.release()

    async def put_stream(
        self,
        path: str,
        chunks: AsyncIterator[bytes],
        *,
        size_bytes: int,
        content_type: str,
    ) -> UploadReceipt:
        safe_path = safe_storage_relpath(path)
        if size_bytes < 0 or not _CONTENT_TYPE_RE.fullmatch(content_type):
            raise ValueError("invalid WebDAV upload metadata")
        transferred = 0

        async def counted() -> AsyncIterator[bytes]:
            nonlocal transferred
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise WebDavWriteError("put", "invalid_stream")
                transferred += len(chunk)
                if transferred > size_bytes:
                    raise WebDavWriteError("put", "size_mismatch")
                if chunk:
                    yield chunk

        response = await self._request(
            "PUT", safe_path, operation="put",
            headers={"Content-Length": str(size_bytes), "Content-Type": content_type},
            data=counted(),
        )
        try:
            self._check(response.status, {200, 201, 204}, "put")
            if transferred != size_bytes:
                raise WebDavWriteError("put", "size_mismatch", response.status)
            return UploadReceipt(response.status, transferred, response.headers.get("ETag"))
        finally:
            response.release()

    async def delete(self, path: str) -> DeleteReceipt:
        response = await self._request("DELETE", safe_storage_relpath(path), operation="delete")
        try:
            self._check(response.status, {200, 202, 204, 404}, "delete")
            return DeleteReceipt(response.status, True)
        finally:
            response.release()

    async def stat(self, path: str) -> RemoteFileStat | None:
        response = await self._request("HEAD", safe_storage_relpath(path), operation="stat")
        try:
            if response.status == 404:
                return None
            self._check(response.status, {200, 204}, "stat")
            length = response.headers.get("Content-Length")
            try:
                size_bytes = int(length) if length is not None else 0
            except ValueError:
                raise WebDavWriteError("stat", "invalid_response", response.status) from None
            if size_bytes < 0:
                raise WebDavWriteError("stat", "invalid_response", response.status)
            return RemoteFileStat(size_bytes, response.headers.get("ETag"))
        finally:
            response.release()

    async def move(self, source: str, target: str, *, overwrite: bool) -> None:
        source_path = safe_storage_relpath(source)
        target_path = safe_storage_relpath(target)
        response = await self._request(
            "MOVE", source_path, operation="move",
            headers={
                "Destination": self._url(target_path),
                "Overwrite": "T" if overwrite else "F",
            },
        )
        try:
            if response.status in {405, 501}:
                raise WebDavWriteError("move", "move_unsupported", response.status)
            self._check(response.status, {201, 204}, "move")
        finally:
            response.release()

    async def _request(self, method: str, path: str, *, operation: str, **kwargs: object):
        await self.open()
        assert self._session is not None
        try:
            return await self._session.request(  # type: ignore[attr-defined]
                method, self._url(path), allow_redirects=False, **kwargs,
            )
        except WebDavWriteError:
            raise
        except Exception as exc:
            raise WebDavWriteError(operation, "network_error") from exc

    def _url(self, path: str) -> str:
        safe_path = safe_storage_relpath(path)
        return f"{self._base_url}/{quote(safe_path, safe='/')}"

    @staticmethod
    def _check(status: int, accepted: set[int], operation: str) -> None:
        if status in accepted:
            return
        if 300 <= status < 400:
            category = "unsafe_redirect"
        elif status in {401, 403}:
            category = "unauthorized"
        elif status == 429:
            category = "rate_limited"
        elif status >= 500:
            category = "server_error"
        elif status in {404, 409}:
            category = "not_found_or_conflict"
        else:
            category = "rejected"
        raise WebDavWriteError(operation, category, status)
