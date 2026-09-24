"""Privacy-conscious structured diagnostics for the Player HTTP boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from typing import Any


_LOG = logging.getLogger("tgvio_player.diagnostics")
_CLIENT_LOG_SALT = secrets.token_bytes(32)


def fingerprint(value: str, *, length: int = 12) -> str:
    """Return a stable, non-reversible identifier suitable for log correlation."""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def client_fingerprint(value: str, *, length: int = 12) -> str:
    """Return a process-scoped pseudonym; raw IP addresses never enter logs."""
    return hmac.new(_CLIENT_LOG_SALT, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()[:length]


def log_event(event: str, **fields: Any) -> None:
    """Write one compact JSON event; callers must pass only reviewed fields."""
    payload = {"event": event, **fields}
    _LOG.info("player_event %s", json.dumps(payload, separators=(",", ":"), sort_keys=True))
