"""Small authenticated HTTP surface for the localhost-only O1 dashboard."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import stat
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .repository import RepositoryError
from .security import secure_private_directory


logger = logging.getLogger(__name__)

_MAX_HEADERS = 16 * 1024
_MAX_REQUEST_LINE = 2048
_READ_TIMEOUT = 5.0
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class DashboardProtocolError(ValueError):
    def __init__(self, status: HTTPStatus, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class DashboardServer:
    """Serve a bounded GET-only API over a private Unix socket or loopback TCP."""

    def __init__(
        self,
        service: Any,
        *,
        token: str,
        host: str = "127.0.0.1",
        port: int = 8787,
        socket_path: str = "session/dashboard.sock",
        html_path: str | os.PathLike[str] | None = None,
    ) -> None:
        encoded_token = str(token).encode("utf-8")
        if (
            len(encoded_token) < 32
            or len(encoded_token) > 512
            or any(byte < 33 or byte > 126 for byte in encoded_token)
        ):
            raise ValueError("dashboard token must contain at least 32 bytes")
        self._service = service
        self._token = str(token)
        self._host = str(host)
        self._port = int(port)
        self._socket_path = Path(socket_path) if socket_path else None
        self._html_path = (
            Path(html_path)
            if html_path is not None
            else Path(__file__).resolve().parents[1] / "demo" / "o1-dashboard-taste.html"
        )
        self._server: asyncio.AbstractServer | None = None

    @property
    def serving(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def sockets(self):
        return tuple(self._server.sockets or ()) if self._server is not None else ()

    async def start(self) -> None:
        if self._server is not None:
            return
        if not self._html_path.is_file():
            raise RuntimeError("dashboard HTML asset is unavailable")
        if self._socket_path is not None:
            path = self._socket_path
            if not secure_private_directory(path.parent):
                raise RuntimeError("dashboard socket directory is unavailable or unsafe")
            if path.exists() or path.is_symlink():
                mode = path.lstat().st_mode
                if not stat.S_ISSOCK(mode):
                    raise RuntimeError("dashboard socket path exists and is not a socket")
                path.unlink()
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                path=str(path),
                limit=_MAX_HEADERS + 1,
            )
            os.chmod(path, 0o600)
            logger.info("Dashboard listening on a private Unix socket")
            return
        if self._host not in {"127.0.0.1", "::1"}:
            raise RuntimeError("dashboard TCP listener must use a literal loopback address")
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._host,
            port=self._port,
            limit=_MAX_HEADERS + 1,
        )
        logger.info("Dashboard listening on loopback TCP port %d", self._port)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        if self._socket_path is not None and self._socket_path.exists():
            try:
                if stat.S_ISSOCK(self._socket_path.lstat().st_mode):
                    self._socket_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _parse_headers(raw: bytes) -> tuple[str, str, dict[str, str]]:
        if len(raw) > _MAX_HEADERS:
            raise DashboardProtocolError(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE, "headers_too_large")
        try:
            lines = raw[:-4].decode("iso-8859-1").split("\r\n")
        except UnicodeDecodeError as exc:
            raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_headers") from exc
        if not lines or len(lines[0]) > _MAX_REQUEST_LINE:
            raise DashboardProtocolError(HTTPStatus.URI_TOO_LONG, "request_line_too_large")
        parts = lines[0].split(" ")
        if len(parts) != 3 or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}:
            raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_request_line")
        method, target, _version = parts
        if method not in {"GET", "HEAD"}:
            raise DashboardProtocolError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")
        if not target.startswith("/") or target.startswith("//"):
            raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_target")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_header")
            name, value = line.split(":", 1)
            lowered = name.strip().lower()
            if not lowered or not _HEADER_NAME.fullmatch(name.strip()) or lowered in headers:
                raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_header")
            if "\r" in value or "\n" in value:
                raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_header")
            headers[lowered] = value.strip()
        if "transfer-encoding" in headers:
            raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "request_body_not_allowed")
        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError as exc:
            raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_content_length") from exc
        if content_length != 0:
            raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "request_body_not_allowed")
        return method, target, headers

    def _authorized(self, headers: dict[str, str]) -> bool:
        supplied = headers.get("authorization", "")
        expected = f"Bearer {self._token}"
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")

    async def _dispatch(
        self, method: str, target: str, headers: dict[str, str]
    ) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
        parsed = urlsplit(target)
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_query") from exc
        if any(key.lower() in {"token", "access_token", "authorization", "auth"} for key in query):
            raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "query_credentials_forbidden")
        if parsed.path in {"/", "/dashboard"}:
            if query:
                raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "unexpected_query")
            return HTTPStatus.OK, "text/html; charset=utf-8", self._html_path.read_bytes(), {}
        if parsed.path == "/favicon.ico":
            return HTTPStatus.NO_CONTENT, "image/x-icon", b"", {}
        if not self._authorized(headers):
            body = self._json_bytes({"error": "unauthorized", "schema_version": 1})
            return (
                HTTPStatus.UNAUTHORIZED,
                "application/json; charset=utf-8",
                body,
                {"WWW-Authenticate": 'Bearer realm="tvf-dashboard"'},
            )
        if parsed.path == "/api/v1/overview":
            if query:
                raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "unexpected_query")
            value = await self._service.overview()
            return HTTPStatus.OK, "application/json; charset=utf-8", self._json_bytes(value), {}
        if parsed.path == "/api/v1/jobs":
            allowed = {"filter", "page", "page_size"}
            if set(query) - allowed or any(len(values) != 1 for values in query.values()):
                raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_query")
            try:
                page = int(query.get("page", ["0"])[0])
                page_size = int(query.get("page_size", ["20"])[0])
            except ValueError as exc:
                raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "invalid_query") from exc
            value = await self._service.jobs(
                filter_name=query.get("filter", ["all"])[0],
                page=page,
                page_size=page_size,
            )
            return HTTPStatus.OK, "application/json; charset=utf-8", self._json_bytes(value), {}
        routes = {
            "/api/v1/routing": self._service.routing,
            "/api/v1/storage": self._service.storage,
            "/api/v1/health": self._service.health,
        }
        if parsed.path in routes:
            if query:
                raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "unexpected_query")
            value = await routes[parsed.path]()
            return HTTPStatus.OK, "application/json; charset=utf-8", self._json_bytes(value), {}
        if parsed.path == "/metrics":
            if query:
                raise DashboardProtocolError(HTTPStatus.BAD_REQUEST, "unexpected_query")
            value = (await self._service.metrics()).encode("utf-8")
            return HTTPStatus.OK, "text/plain; version=0.0.4; charset=utf-8", value, {}
        raise DashboardProtocolError(HTTPStatus.NOT_FOUND, "not_found")

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        *,
        method: str,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        headers = {
            "Connection": "close",
            "Content-Length": str(len(body)),
            "Content-Type": content_type,
            **_SECURITY_HEADERS,
            **(extra_headers or {}),
        }
        head = [f"HTTP/1.1 {status.value} {status.phrase}\r\n"]
        head.extend(f"{name}: {value}\r\n" for name, value in headers.items())
        head.append("\r\n")
        writer.write("".join(head).encode("iso-8859-1"))
        if method != "HEAD" and status != HTTPStatus.NO_CONTENT:
            writer.write(body)
        await writer.drain()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        method = "GET"
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=_READ_TIMEOUT)
            method, target, headers = self._parse_headers(raw)
            status, content_type, body, extra = await self._dispatch(method, target, headers)
        except asyncio.TimeoutError:
            status, content_type, body, extra = (
                HTTPStatus.REQUEST_TIMEOUT,
                "application/json; charset=utf-8",
                self._json_bytes({"error": "request_timeout", "schema_version": 1}),
                {},
            )
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            status, content_type, body, extra = (
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                "application/json; charset=utf-8",
                self._json_bytes({"error": "headers_too_large", "schema_version": 1}),
                {},
            )
        except DashboardProtocolError as exc:
            status, content_type, body, extra = (
                exc.status,
                "application/json; charset=utf-8",
                self._json_bytes({"error": exc.code, "schema_version": 1}),
                {"Allow": "GET, HEAD"} if exc.status == HTTPStatus.METHOD_NOT_ALLOWED else {},
            )
        except RepositoryError:
            status, content_type, body, extra = (
                HTTPStatus.BAD_REQUEST,
                "application/json; charset=utf-8",
                self._json_bytes({"error": "invalid_request", "schema_version": 1}),
                {},
            )
        except Exception:
            logger.exception("Dashboard request failed")
            status, content_type, body, extra = (
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "application/json; charset=utf-8",
                self._json_bytes({"error": "internal_error", "schema_version": 1}),
                {},
            )
        try:
            await self._send(
                writer,
                method=method,
                status=status,
                content_type=content_type,
                body=body,
                extra_headers=extra,
            )
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass
