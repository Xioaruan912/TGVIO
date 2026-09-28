"""Privacy-conscious structured diagnostics for the Player HTTP boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from typing import Any


_LOG = logging.getLogger("tgvio_player.diagnostics")
_CLIENT_LOG_SALT = secrets.token_bytes(32)
_PLAYBACK_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")


def fingerprint(value: str, *, length: int = 12) -> str:
    """Return a stable, non-reversible identifier suitable for log correlation."""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def client_fingerprint(value: str, *, length: int = 12) -> str:
    """Return a process-scoped pseudonym; raw IP addresses never enter logs."""
    return hmac.new(_CLIENT_LOG_SALT, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()[:length]


def playback_session(value: object) -> str | None:
    """Keep only bounded opaque session IDs that can correlate browser events."""
    return value if isinstance(value, str) and _PLAYBACK_SESSION_RE.fullmatch(value) else None


def classify_http_outcome(status: int, error: str | None, *, is_stream: bool) -> str:
    """Separate expected range traffic and client aborts from playback failures."""
    if error == "RequestCancelled" or status == 499:
        return "client_cancelled"
    if error in {
        "BrokenPipeError",
        "ConnectionError",
        "ConnectionResetError",
        "ClientConnectionError",
        "ClientOSError",
    }:
        return "client_disconnected"
    if status == 404:
        return "not_found"
    if status == 416:
        return "invalid_range"
    if status == 429:
        return "capacity_limited"
    if status >= 500:
        return "server_error"
    if status >= 400:
        return "request_error"
    if is_stream and status in {200, 206}:
        return "stream_ok"
    return "success"


def log_event(event: str, **fields: Any) -> None:
    """Write one compact JSON event; callers must pass only reviewed fields."""
    payload = {"event": event, **fields}
    _LOG.info("player_event %s", json.dumps(payload, separators=(",", ":"), sort_keys=True))
