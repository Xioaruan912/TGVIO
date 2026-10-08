"""Read archive media straight from the storage CDN, with links from OpenList.

OpenList already owns the cloud login, its renewal and the mounted folders. The
Player asks its API (with the same read-only account it uses for WebDAV) for a
file's direct link, keeps that link until shortly before it expires, and reads
ranges from the CDN over one kept-alive connection pool. A fresh TLS connection to
a distant CDN costs seconds; a reused one costs about one round trip, which is
what makes seeking fast. The Player never holds the cloud account's credentials.

Links are bound to the User-Agent they were requested with, so the same fixed
User-Agent is sent to OpenList and to the CDN.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, TCPConnector

from tgvio_player.domain.catalog import safe_remote_path
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.infrastructure.webdav_aiohttp import _ResponseBody
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; TGVIO-Player/1.0)"
# A link without an expiry stamp is trusted for this long.
_FALLBACK_LINK_SECONDS = 600
# Refresh this long before the stamped expiry, so a read never starts on a dying link.
_EXPIRY_MARGIN_SECONDS = 120


class DirectReadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpenListDirectSettings:
    api_url: str
    username: str
    password: str
    user_agent: str = DEFAULT_USER_AGENT
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 30.0
    max_connections_per_host: int = 8


@dataclass(frozen=True, slots=True)
class _Link:
    url: str
    expires_at: float


class OpenListDirectReader:
    def __init__(self, settings: OpenListDirectSettings, *, clock: Callable[[], float] = time.time) -> None:
        parsed = urlsplit(settings.api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("OpenList API URL must be http(s) without userinfo")
        self._settings = settings
        self._api = settings.api_url.rstrip("/")
        self._clock = clock
        self._session: ClientSession | None = None
        self._token: str | None = None
        self._token_lock = asyncio.Lock()
        self._links: dict[str, _Link] = {}
        self._link_locks: dict[str, asyncio.Lock] = {}
        self.link_fetches = 0
        self.link_hits = 0
        self.link_refreshes = 0

    async def open(self) -> None:
        if self._session is None:
            self._session = ClientSession(
                connector=TCPConnector(
                    limit_per_host=self._settings.max_connections_per_host,
                    keepalive_timeout=90,
                ),
                timeout=ClientTimeout(
                    total=None,
                    connect=self._settings.connect_timeout_seconds,
                    sock_read=self._settings.read_timeout_seconds,
                ),
                headers={"User-Agent": self._settings.user_agent},
                # The CDN link carries its own signature; no cookies are kept.
                cookie_jar=DummyCookieJar(),
            )

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    def stats(self) -> dict[str, int]:
        return {
            "links_cached": len(self._links),
            "link_fetches": self.link_fetches,
            "link_hits": self.link_hits,
            "link_refreshes": self.link_refreshes,
        }

    async def open_range(
        self, package_path: str, remote_relpath: str, byte_range: ByteRange | None
    ) -> WebDavRangeResponse:
        package = safe_remote_path(package_path, relative=False)
        relpath = safe_remote_path(remote_relpath, relative=True)
        path = f"/{package}/{relpath}"
        for attempt in range(2):
            link = await self._link(path, force=attempt > 0)
            response = await self._get(link.url, byte_range)
            # 403/410: the CDN no longer honours the link (expired early or rebound).
            if response.status in {403, 410} and attempt == 0:
                response.release()
                self.link_refreshes += 1
                continue
            if response.status not in {200, 206, 416}:
                status = response.status
                response.release()
                raise DirectReadError(f"direct read failed ({status})")
            length = response.headers.get("Content-Length")
            try:
                content_length = int(length) if length is not None else None
            except ValueError:
                content_length = None
            return WebDavRangeResponse(
                response.status,
                response.headers.get("Content-Type"),
                content_length,
                response.headers.get("Content-Range"),
                response.headers.get("ETag"),
                _ResponseBody(response),
            )
        raise DirectReadError("direct link refused after refresh")

    async def _get(self, url: str, byte_range: ByteRange | None):
        await self.open()
        assert self._session is not None
        headers = {} if byte_range is None else {"Range": f"bytes={byte_range.start}-{byte_range.end}"}
        return await self._session.get(url, headers=headers, allow_redirects=True)

    async def _link(self, path: str, *, force: bool) -> _Link:
        now = self._clock()
        cached = self._links.get(path)
        if cached is not None and not force and cached.expires_at > now:
            self.link_hits += 1
            return cached
        lock = self._link_locks.setdefault(path, asyncio.Lock())
        async with lock:
            cached = self._links.get(path)
            if cached is not None and not force and cached.expires_at > self._clock():
                self.link_hits += 1
                return cached
            if force:
                self._links.pop(path, None)
            link = await self._fetch_link(path)
            self._links[path] = link
            self._prune()
            return link

    async def _fetch_link(self, path: str) -> _Link:
        for attempt in range(2):
            token = await self._login(force=attempt > 0)
            payload = await self._api_post("/api/fs/get", {"path": path}, token)
            code = payload.get("code")
            if code == 401 and attempt == 0:
                continue
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            url = data.get("raw_url") if isinstance(data, dict) else None
            if code != 200 or not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise DirectReadError(f"no direct link (code {code})")
            self.link_fetches += 1
            return _Link(url, self._expiry(url))
        raise DirectReadError("OpenList login rejected")

    def _expiry(self, url: str) -> float:
        now = self._clock()
        stamp = parse_qs(urlsplit(url).query).get("t", [""])[0]
        if stamp.isdigit():
            expires = int(stamp) - _EXPIRY_MARGIN_SECONDS
            # A stamp already in the past (clock skew) still allows one short use.
            return expires if expires > now else now + 30
        return now + _FALLBACK_LINK_SECONDS

    def _prune(self) -> None:
        now = self._clock()
        for key in [key for key, link in self._links.items() if link.expires_at <= now]:
            self._links.pop(key, None)
            self._link_locks.pop(key, None)

    async def _login(self, *, force: bool) -> str:
        async with self._token_lock:
            if self._token is not None and not force:
                return self._token
            payload = await self._api_post(
                "/api/auth/login",
                {"username": self._settings.username, "password": self._settings.password},
                None,
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            token = data.get("token") if isinstance(data, dict) else None
            if payload.get("code") != 200 or not isinstance(token, str) or not token:
                raise DirectReadError("OpenList login failed")
            self._token = token
            return token

    async def _api_post(self, route: str, body: dict[str, object], token: str | None) -> dict[str, object]:
        await self.open()
        assert self._session is not None
        headers = {"Authorization": token} if token else {}
        async with self._session.post(self._api + route, json=body, headers=headers) as response:
            if response.status != 200:
                raise DirectReadError(f"OpenList API failed ({response.status})")
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise DirectReadError("OpenList API returned an invalid payload")
        return payload
