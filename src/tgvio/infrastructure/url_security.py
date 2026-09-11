from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from typing import Callable
from urllib.parse import parse_qsl, urlsplit


@dataclass(frozen=True, slots=True)
class UrlRisk:
    hostname: str
    private_network: bool
    addresses: tuple[str, ...]


Resolver = Callable[..., list[tuple]]

_SECRET_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "key",
    "password",
    "pass",
    "secret",
    "signature",
    "token",
}


def validate_url_syntax(url: str) -> tuple[str, int]:
    """Validate one durable HTTP(S) source without doing DNS/network I/O."""
    raw = str(url or "").strip()
    if not raw or any(character.isspace() for character in raw):
        raise ValueError("URL must be one compact HTTP(S) address")
    try:
        parsed = urlsplit(raw)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValueError("URL has an invalid port") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use HTTP(S) with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not accepted")
    if parsed.fragment:
        raise ValueError("URL fragments are not accepted")
    sensitive = {
        key.lower()
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() in _SECRET_QUERY_KEYS
    }
    if sensitive:
        raise ValueError("URL contains credential-like query parameters")
    return parsed.hostname.rstrip(".").lower(), port


def validate_download_url(
    url: str,
    *,
    private_network_policy: str = "block",
    resolver: Resolver = socket.getaddrinfo,
) -> UrlRisk:
    hostname, port = validate_url_syntax(url)
    policy = str(private_network_policy or "block").strip().lower()
    if policy not in {"allow", "warn", "block"}:
        raise ValueError("invalid private network URL policy")
    if policy == "allow":
        return UrlRisk(hostname=hostname, private_network=False, addresses=())

    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(hostname)))
    except ValueError:
        try:
            for result in resolver(hostname, port, type=socket.SOCK_STREAM):
                sockaddr = result[4]
                if sockaddr:
                    addresses.add(str(ipaddress.ip_address(sockaddr[0])))
        except OSError as exc:
            if policy == "warn":
                return UrlRisk(hostname=hostname, private_network=False, addresses=())
            raise ValueError("URL hostname resolution failed") from exc
    if not addresses:
        if policy == "warn":
            return UrlRisk(hostname=hostname, private_network=False, addresses=())
        raise ValueError("URL hostname did not resolve")
    private = any(not ipaddress.ip_address(value).is_global for value in addresses)
    if private and policy == "block":
        raise ValueError("private/local URL target blocked")
    return UrlRisk(
        hostname=hostname,
        private_network=private,
        addresses=tuple(sorted(addresses)),
    )
