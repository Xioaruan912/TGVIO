from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import urllib.parse

from tgvio.application.dashboard import DashboardService
from tgvio.application.metrics import MetricsService
from tgvio.observability import log_event


_MAX_HEADER_BYTES = 16 * 1024
_SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)
_STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
}

_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TGVIO read-only</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:48rem}pre{background:#f4f4f5;padding:1rem;overflow:auto}</style>
</head><body>
<h1>TGVIO read-only console</h1>
<p>Enter the dashboard token to load redacted operational data. The token is kept only in this tab's session storage.</p>
<input id="token" type="password" placeholder="bearer token" autocomplete="off">
<button id="load">Load</button>
<pre id="out">idle</pre>
<script>
const out=document.getElementById('out');
async function load(){
  const token=sessionStorage.getItem('tgvio_token')||'';
  const headers=token?{'Authorization':'Bearer '+token}:{};
  try{const r=await fetch('/api/v1/overview',{headers});const t=await r.text();
    out.textContent=r.status+' '+r.statusText+'\\n\\n'+t;}catch(e){out.textContent=String(e);}
}
document.getElementById('load').onclick=()=>{const v=document.getElementById('token').value;sessionStorage.setItem('tgvio_token',v);load();};
if(sessionStorage.getItem('tgvio_token'))load();
</script>
</body></html>
"""


class DashboardServer:
    """Minimal loopback-only, read-only HTTP/1.1 server (stdlib asyncio)."""

    def __init__(
        self,
        service: DashboardService,
        metrics: MetricsService,
        *,
        host: str,
        port: int,
        token: str,
    ) -> None:
        if not _is_loopback(host):
            raise ValueError("dashboard must bind to a loopback address")
        if len(token) < 32:
            raise ValueError("dashboard token must be at least 32 characters")
        self._service = service
        self._metrics = metrics
        self._host = host
        self._port = int(port)
        self._token = token
        self._server: asyncio.AbstractServer | None = None
        self._log = logging.getLogger("tgvio.web.dashboard")

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self._port
        sockets = self._server.sockets or ()
        if sockets:
            return int(sockets[0].getsockname()[1])
        return self._port

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        log_event(
            self._log,
            logging.INFO,
            "web.dashboard.started",
            "Read-only dashboard listener started",
            host_class="loopback",
        )

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await self._handle_request(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak internal detail
            log_event(
                self._log,
                logging.WARNING,
                "web.dashboard.request_failed",
                exception_type=type(exc).__name__,
            )
            await _write_response(writer, 400, "application/json", b'{"error":"bad_request"}', head=False)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        except asyncio.LimitOverrunError:
            await _write_response(writer, 413, "application/json", b'{"error":"header_too_large"}', head=False)
            return
        except (asyncio.IncompleteReadError, TimeoutError):
            return
        if len(raw) > _MAX_HEADER_BYTES:
            await _write_response(writer, 413, "application/json", b'{"error":"header_too_large"}', head=False)
            return
        lines = raw.decode("latin-1").split("\r\n")
        request_line = lines[0].split(" ")
        if len(request_line) != 3 or not request_line[2].startswith("HTTP/"):
            await _write_response(writer, 400, "application/json", b'{"error":"bad_request"}', head=False)
            return
        method, target, _version = request_line
        headers = _parse_headers(lines[1:])
        head = method == "HEAD"
        if method not in {"GET", "HEAD"}:
            await _write_response(writer, 405, "application/json", b'{"error":"method_not_allowed"}', head=head)
            return
        if headers.get("content-length") not in (None, "0"):
            await _write_response(writer, 400, "application/json", b'{"error":"body_not_allowed"}', head=head)
            return

        parsed = urllib.parse.urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            await _write_response(writer, 200, "text/html; charset=utf-8", _INDEX_HTML.encode("utf-8"), head=head)
            return

        if path == "/metrics" or path.startswith("/api/"):
            if "token" in query or "access_token" in query:
                await _write_response(writer, 400, "application/json", b'{"error":"query_token_rejected"}', head=head)
                return
            if not self._authorized(headers.get("authorization", "")):
                await _write_response(
                    writer,
                    401,
                    "application/json",
                    b'{"error":"unauthorized"}',
                    head=head,
                    extra_headers=(("WWW-Authenticate", "Bearer"),),
                )
                return

        try:
            if path == "/metrics":
                body = (await self._metrics.render()).encode("utf-8")
                await _write_response(writer, 200, "text/plain; version=0.0.4; charset=utf-8", body, head=head)
                return
            if path == "/api/v1/overview":
                await self._json(writer, await self._service.overview(), head=head)
                return
            if path == "/api/v1/jobs":
                filter_value = (query.get("filter") or ["all"])[0]
                page = _safe_int((query.get("page") or ["0"])[0], default=0)
                await self._json(
                    writer,
                    await self._service.jobs(filter=filter_value, page=page),
                    head=head,
                )
                return
            if path == "/api/v1/routing":
                await self._json(writer, await self._service.routing(), head=head)
                return
            if path == "/api/v1/storage":
                await self._json(writer, await self._service.storage(), head=head)
                return
            if path == "/api/v1/health":
                await self._json(writer, await self._service.health(), head=head)
                return
        except Exception as exc:  # noqa: BLE001 - normalized
            log_event(
                self._log,
                logging.ERROR,
                "web.dashboard.query_failed",
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            await _write_response(writer, 400, "application/json", b'{"error":"upstream_unavailable"}', head=head)
            return

        await _write_response(writer, 404, "application/json", b'{"error":"not_found"}', head=head)

    def _authorized(self, header: str) -> bool:
        if not header.startswith("Bearer "):
            return False
        presented = header[len("Bearer ") :].strip()
        return hmac.compare_digest(presented.encode("utf-8"), self._token.encode("utf-8"))

    async def _json(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, object],
        *,
        head: bool,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        await _write_response(writer, 200, "application/json", body, head=head)


def _is_loopback(host: str) -> bool:
    candidate = (host or "").strip().lower()
    if candidate in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _parse_headers(lines: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers


def _safe_int(value: str, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


async def _write_response(
    writer: asyncio.StreamWriter,
    status: int,
    content_type: str,
    body: bytes,
    *,
    head: bool,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> None:
    reason = _STATUS_TEXT.get(status, "OK")
    headers = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    headers.extend(f"{name}: {value}" for name, value in _SECURITY_HEADERS)
    headers.extend(f"{name}: {value}" for name, value in extra_headers)
    payload = ("\r\n".join(headers) + "\r\n\r\n").encode("latin-1")
    writer.write(payload)
    if not head:
        writer.write(body)
    try:
        await writer.drain()
    except (ConnectionError, RuntimeError):
        pass
