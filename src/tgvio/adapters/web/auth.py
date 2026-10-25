"""Owner login for the web console: one access secret, opaque session cookies.

Sessions live in memory (one owner; a restart asks for the secret again), only
their SHA-256 digests are kept, and failed logins are rate-limited per client.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections.abc import Callable

from aiohttp import web

COOKIE = "tgvio_console"
SESSION_SECONDS = 12 * 60 * 60
_MAX_SESSIONS = 32
_FAILURE_LIMIT = 5
_FAILURE_WINDOW_SECONDS = 15 * 60
_LOCKOUT_SECONDS = 15 * 60
_TRUSTED_PROXIES = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ConsoleSessions:
    def __init__(self, secret: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        if not secret:
            raise ValueError("console secret is required")
        self._secret = secret.encode("utf-8")
        self._clock = clock
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, tuple[int, float, float]] = {}

    def login(self, client: str, supplied: object) -> str | None:
        """A new session token, or None. Raises HTTPTooManyRequests when locked out."""

        now = self._clock()
        count, started, locked_until = self._failures.get(client, (0, now, 0.0))
        if locked_until > now:
            raise web.HTTPTooManyRequests(
                text="too many failed logins", headers={"Retry-After": str(int(locked_until - now) + 1)}
            )
        if isinstance(supplied, str) and hmac.compare_digest(self._secret, supplied.encode("utf-8")):
            self._failures.pop(client, None)
            self._expire(now)
            if len(self._sessions) >= _MAX_SESSIONS:
                self._sessions.pop(min(self._sessions, key=self._sessions.__getitem__))
            token = secrets.token_urlsafe(32)
            self._sessions[_digest(token)] = now + SESSION_SECONDS
            return token
        if now - started >= _FAILURE_WINDOW_SECONDS:
            count, started = 0, now
        count += 1
        if count >= _FAILURE_LIMIT:
            self._failures[client] = (0, now, now + _LOCKOUT_SECONDS)
        else:
            self._failures[client] = (count, started, 0.0)
        return None

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        expires = self._sessions.get(_digest(token))
        if expires is None:
            return False
        if expires <= self._clock():
            self._sessions.pop(_digest(token), None)
            return False
        return True

    def logout(self, token: str | None) -> None:
        if token:
            self._sessions.pop(_digest(token), None)

    def _expire(self, now: float) -> None:
        for key in [key for key, expires in self._sessions.items() if expires <= now]:
            del self._sessions[key]


def set_cookie_value(token: str) -> str:
    return f"{COOKIE}={token}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Strict"


def clear_cookie_value() -> str:
    return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"


def resolve_client(request: web.Request) -> str:
    """The visitor behind the local nginx (only trusted proxies may say who it is)."""

    peer = request.remote or "unknown"
    try:
        trusted = any(ipaddress.ip_address(peer) in network for network in _TRUSTED_PROXIES)
    except ValueError:
        trusted = False
    if not trusted:
        return peer
    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    if real_ip:
        return real_ip
    hops = [hop.strip() for hop in request.headers.get("X-Forwarded-For", "").split(",") if hop.strip()]
    return hops[-1] if hops else peer
