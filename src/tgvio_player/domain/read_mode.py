"""How the Player reads archive media."""

from __future__ import annotations

WEBDAV = "webdav"
DIRECT = "direct"
READ_MODES = (WEBDAV, DIRECT)
DEFAULT_READ_MODE = WEBDAV


def parse_read_mode(value: object) -> str:
    if value not in READ_MODES:
        raise ValueError("read mode must be 'webdav' or 'direct'")
    return str(value)
