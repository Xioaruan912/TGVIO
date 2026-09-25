from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import quote, unquote, urlsplit


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class PlayerStorageSettings:
    endpoint_url: str
    player_root: str
    favorites_dir: str
    username_ciphertext: bytes | None = None
    password_ciphertext: bytes | None = None
    revision: int = 0


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
    ):
        raise ValueError("WebDAV endpoint must be a public HTTPS URL without userinfo")
    decoded_path = unquote(parsed.path)
    if (
        "\\" in decoded_path or _CONTROL_RE.search(decoded_path)
        or "//" in decoded_path
        or any(part in {".", ".."} for part in decoded_path.split("/"))
    ):
        raise ValueError("WebDAV endpoint has an unsafe path")
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
    safe_path = quote(decoded_path.rstrip("/"), safe="/:@!$&'()*+,;=-._~")
    return f"https://{authority}{safe_path}"


def safe_storage_relpath(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ValueError("unsafe WebDAV relative path")
    if "\\" in value or _CONTROL_RE.search(value):
        raise ValueError("unsafe WebDAV relative path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("unsafe WebDAV relative path")
    return value
