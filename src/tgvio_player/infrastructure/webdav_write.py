from __future__ import annotations

from collections.abc import AsyncIterator
import json
from pathlib import PurePosixPath
import re
from urllib.parse import quote, urlsplit

from aiohttp import BasicAuth, ClientSession, ClientTimeout, TCPConnector

from tgvio_player.application.ports import DeleteReceipt, RemoteFileStat, UploadReceipt, WebDavWriteError
from tgvio_player.domain.storage_settings import safe_storage_relpath, validate_webdav_endpoint
from tgvio_player.infrastructure.webdav_aiohttp import PublicOnlyResolver
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse


_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


class AioHttpWebDavWriteClient:
    """A validated WebDAV write client with streamed request bodies."""

    def __init__(
        self,
        endpoint_url: str,
        username: str,
        password: str,
        *,
        session: object | None = None,
        api_session: object | None = None,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = validate_webdav_endpoint(endpoint_url)
        if min(connect_timeout_seconds, read_timeout_seconds) <= 0:
            raise ValueError("WebDAV timeouts must be positive")
        self._auth = BasicAuth(username, password)
        self._username = username
        self._password = password
        self._timeout = ClientTimeout(
            total=None, connect=connect_timeout_seconds, sock_read=read_timeout_seconds,
        )
        self._session = session
        self._owns_session = session is None
        self._api_session = api_session
        self._owns_api_session = api_session is None

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
        if self._owns_api_session and self._api_session is not None:
            await self._api_session.close()  # type: ignore[attr-defined]
        self._api_session = None

    async def copy(self, source: str, target: str) -> None:
        safe_source = safe_storage_relpath(source)
        safe_target = safe_storage_relpath(target)
        source_name = PurePosixPath(safe_source).name
        if PurePosixPath(safe_target).name != source_name:
            raise WebDavWriteError("copy", "copy_unsupported")
        endpoint = urlsplit(self._base_url)
        if endpoint.path.rstrip("/") != "/dav":
            raise WebDavWriteError("copy", "copy_unsupported")
        api_base = f"{endpoint.scheme}://{endpoint.netloc}"
        login = await self._api_request(
            "POST", f"{api_base}/api/auth/login",
            json={"username": self._username, "password": self._password},
        )
        try:
            login_payload = json.loads(await login.read())
            self._check(login.status, {200}, "copy")
            if login_payload.get("code") != 200 or not isinstance(
                (login_payload.get("data") or {}).get("token"), str
            ):
                raise WebDavWriteError("copy", "unauthorized", login.status)
            token = login_payload["data"]["token"]
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            raise WebDavWriteError("copy", "copy_unsupported", login.status) from None
        finally:
            login.release()
        copied = await self._api_request(
            "POST", f"{api_base}/api/fs/copy",
            headers={"Authorization": token},
            json={
                "src_dir": f"/{PurePosixPath(safe_source).parent}",
                "dst_dir": f"/{PurePosixPath(safe_target).parent}",
                "names": [source_name],
            },
        )
        try:
            payload = json.loads(await copied.read())
            self._check(copied.status, {200}, "copy")
            code = payload.get("code")
            if code != 200:
                self._check(int(code) if isinstance(code, int) else 400, {200}, "copy")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            raise WebDavWriteError("copy", "invalid_response", copied.status) from None
        finally:
            copied.release()

    async def _api_request(self, method: str, url: str, **kwargs: object):
        if self._api_session is None:
            connector = TCPConnector(
                resolver=PublicOnlyResolver(), use_dns_cache=False, ttl_dns_cache=0,
            )
            self._api_session = ClientSession(timeout=self._timeout, connector=connector)
        try:
            return await self._api_session.request(  # type: ignore[attr-defined]
                method, url, allow_redirects=False, **kwargs,
            )
        except Exception as exc:
            raise WebDavWriteError("copy", "network_error") from exc

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

    async def get_bytes(self, path: str, *, max_bytes: int) -> bytes | None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        response = await self._request("GET", safe_storage_relpath(path), operation="get")
        try:
            if response.status == 404:
                return None
            self._check(response.status, {200}, "get")
            payload = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise WebDavWriteError("get", "response_too_large", response.status)
            return bytes(payload)
        finally:
            response.release()

    async def open_stream(self, path: str) -> tuple[int, str | None, AsyncIterator[bytes]]:
        response = await self._request("GET", safe_storage_relpath(path), operation="get")
        if response.status == 404:
            response.release()
            raise WebDavWriteError("get", "not_found", 404)
        try:
            self._check(response.status, {200}, "get")
            raw_size = response.headers.get("Content-Length")
            size_bytes = int(raw_size) if raw_size is not None else -1
            if size_bytes < 0:
                raise WebDavWriteError("get", "invalid_response", response.status)
        except Exception:
            response.release()
            raise

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    yield chunk
            finally:
                response.release()

        return size_bytes, response.headers.get("Content-Type"), chunks()

    async def open_range(
        self, path: str, byte_range: ByteRange | None
    ) -> WebDavRangeResponse:
        headers = {"Range": f"bytes={byte_range.start}-{byte_range.end}"} if byte_range else {}
        response = await self._request(
            "GET", safe_storage_relpath(path), operation="get", headers=headers,
        )
        if response.status not in {200, 206, 404, 416}:
            response.release()
            self._check(response.status, {200, 206, 404, 416}, "get")

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    yield chunk
            finally:
                response.release()

        raw_length = response.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else None
        except ValueError:
            response.release()
            raise WebDavWriteError("get", "invalid_response", response.status) from None
        return WebDavRangeResponse(
            response.status, response.headers.get("Content-Type"), length,
            response.headers.get("Content-Range"), response.headers.get("ETag"), chunks(),
        )

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
