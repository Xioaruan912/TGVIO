"""Security helpers shared by logging and URL intake boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import logging
import os
from pathlib import Path
import re
import socket
from typing import Callable
from urllib.parse import unquote, urlsplit, urlunsplit


_URL_CREDENTIALS_RE = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@"
)
_AUTH_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:basic|bearer)\s+[^\s,;]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(BOT_TOKEN|API_HASH|WEBDAV_PASS|PASSWORD|PASS|TOKEN|SECRET)\s*[:=]\s*([^\s,;]+)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|key|password|pass|secret)=)[^&\s]+"
)
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def redact_text(value: object) -> str:
    """Redact common credentials without hiding useful hosts/error context."""
    text = str(value)
    text = _URL_CREDENTIALS_RE.sub(r"\1***@", text)
    text = _AUTH_RE.sub(r"\1***", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = _QUERY_SECRET_RE.sub(r"\1***", text)
    return _TELEGRAM_TOKEN_RE.sub("***", text)


class RedactingFormatter(logging.Formatter):
    """Redact after normal formatting so exception tracebacks are covered too."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def install_redacting_logging(*, fmt: str, root: logging.Logger | None = None) -> None:
    logger = root or logging.getLogger()
    for handler in logger.handlers:
        handler.setFormatter(RedactingFormatter(fmt))


def safe_url_label(value: object) -> str:
    """Render only a credential-free HTTP(S) endpoint label.

    Query strings and fragments are deliberately omitted because signed URLs
    commonly carry credentials there. Invalid values never fall back to the
    original input.
    """
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return "（地址无效）"
        port = parsed.port
    except (TypeError, ValueError):
        return "（地址无效）"
    host = parsed.hostname.rstrip(".").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "", "", ""))


def validate_webdav_url(value: object) -> str:
    """Validate a WebDAV base URL without accepting embedded credentials."""
    raw = str(value or "").strip()
    if not raw or any(character.isspace() for character in raw):
        raise ValueError("WebDAV URL must be a compact http/https URL")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("WebDAV URL has an invalid port") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("WebDAV URL must use http/https with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("WebDAV URL credentials must use separate user/password fields")
    if parsed.query or parsed.fragment:
        raise ValueError("WebDAV base URL cannot contain query or fragment data")
    _validate_path_segments(parsed.path or "/")
    host = parsed.hostname.rstrip(".").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "", "", ""))


def normalize_remote_path(value: object) -> str:
    """Normalize a user-configured relative WebDAV path and reject traversal."""
    segments = _validate_path_segments(str(value or "").strip() or "/")
    return "/" + "/".join(segments) if segments else "/"


def validate_remote_name(value: object) -> str:
    """Accept one WebDAV filename segment, never a path."""
    raw = str(value or "")
    decoded = unquote(raw)
    if (
        not raw
        or raw in {".", ".."}
        or decoded in {".", ".."}
        or "/" in raw
        or "\\" in raw
        or "/" in decoded
        or "\\" in decoded
        or _CONTROL_RE.search(decoded)
    ):
        raise ValueError("invalid remote filename")
    return raw


def sanitize_filename(value: object, *, fallback: str = "media.bin", limit: int = 180) -> str:
    """Turn an untrusted display filename into one local filename segment."""
    text = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    text = _CONTROL_RE.sub("", text).strip().strip(".")
    if not text or text in {".", ".."}:
        text = str(fallback or "media.bin")
    text = text[: max(16, int(limit))]
    return text or "media.bin"


def secure_private_file(path: str | os.PathLike[str]) -> bool:
    """Best-effort chmod for an existing credential/private-data file."""
    target = Path(path)
    try:
        if not target.exists() or target.is_symlink() or not target.is_file():
            return False
        target.chmod(0o600)
        return True
    except OSError:
        return False


def secure_private_directory(path: str | os.PathLike[str]) -> bool:
    """Create or tighten a private runtime directory without following links."""
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or not target.is_dir():
            return False
        target.chmod(0o700)
        return True
    except OSError:
        return False


def _validate_path_segments(value: str) -> tuple[str, ...]:
    decoded_path = unquote(str(value or "/"))
    if "\\" in decoded_path or _CONTROL_RE.search(decoded_path):
        raise ValueError("remote path contains unsupported characters")
    segments: list[str] = []
    for raw_segment in decoded_path.split("/"):
        if not raw_segment:
            continue
        decoded_segment = unquote(raw_segment)
        if (
            decoded_segment in {".", ".."}
            or "/" in decoded_segment
            or "\\" in decoded_segment
            or _CONTROL_RE.search(decoded_segment)
        ):
            raise ValueError("remote path traversal is not allowed")
        segments.append(decoded_segment)
    return tuple(segments)


@dataclass(frozen=True)
class UrlRisk:
    hostname: str
    private_network: bool
    addresses: tuple[str, ...]


Resolver = Callable[..., list[tuple]]


def inspect_http_url(url: str, *, resolver: Resolver = socket.getaddrinfo) -> UrlRisk:
    """Validate HTTP(S) URL and classify directly resolved private/local targets."""
    parsed = urlsplit(str(url).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("unsupported URL: http/https with hostname required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("unsupported URL: credentials are not accepted")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValueError("unsupported URL: invalid port") from exc

    host = parsed.hostname.rstrip(".").lower()
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(host)))
    except ValueError:
        for result in resolver(host, port, type=socket.SOCK_STREAM):
            sockaddr = result[4]
            if sockaddr:
                addresses.add(str(ipaddress.ip_address(sockaddr[0])))
    if not addresses:
        raise ValueError("URL hostname did not resolve")

    private = any(_is_non_public_address(ipaddress.ip_address(value)) for value in addresses)
    return UrlRisk(hostname=host, private_network=private, addresses=tuple(sorted(addresses)))


def enforce_url_policy(
    url: str,
    *,
    private_network_policy: str = "warn",
    resolver: Resolver = socket.getaddrinfo,
) -> UrlRisk:
    policy = str(private_network_policy or "warn").strip().lower()
    if policy not in {"allow", "warn", "block"}:
        raise ValueError("invalid private network URL policy")
    if policy == "allow":
        parsed = urlsplit(str(url).strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("unsupported URL: http/https with hostname required")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("unsupported URL: credentials are not accepted")
        return UrlRisk(hostname=parsed.hostname.rstrip(".").lower(), private_network=False, addresses=())
    try:
        risk = inspect_http_url(url, resolver=resolver)
    except OSError as exc:
        if policy == "warn":
            parsed = urlsplit(str(url).strip())
            return UrlRisk(
                hostname=(parsed.hostname or "unknown").rstrip(".").lower(),
                private_network=False,
                addresses=(),
            )
        raise ValueError("unsupported URL: hostname resolution failed under block policy") from exc
    if risk.private_network and policy == "block":
        raise ValueError("unsupported URL: private-network target blocked")
    return risk


def _is_non_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not address.is_global
