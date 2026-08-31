"""Security helpers shared by logging and URL intake boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import logging
import re
import socket
from typing import Callable
from urllib.parse import urlsplit


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

    host = parsed.hostname.rstrip(".").lower()
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(host)))
    except ValueError:
        for result in resolver(host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM):
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
