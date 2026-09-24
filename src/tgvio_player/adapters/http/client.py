from __future__ import annotations

import ipaddress

from aiohttp import web


_TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_trusted_proxy(host: str) -> bool:
    candidate = (host or "").strip()
    if not candidate:
        return False
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return candidate in {"localhost"}
    return any(address in network for network in _TRUSTED_PROXY_NETWORKS)


def resolve_client(request: web.Request) -> str:
    """Return the real client identity behind the local reverse proxy.

    The Player binds loopback only and is always reached through nginx, so
    ``request.remote`` is the proxy address for every visitor. When (and only
    when) the immediate peer is a trusted private/loopback address we take the
    proxy-appended address: ``X-Real-IP`` if present, otherwise the right-most
    ``X-Forwarded-For`` hop (the one nginx itself added, which a browser cannot
    forge). Direct peers keep their own address.
    """
    peer = request.remote or "unknown"
    if not _is_trusted_proxy(peer):
        return peer
    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    if real_ip:
        return real_ip
    forwarded = request.headers.get("X-Forwarded-For", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if hops:
        return hops[-1]
    return peer


def _prefetch_requested(request: web.Request) -> bool:
    cache = (request.query.get("cache") or "").strip().lower()
    prefetch = (request.query.get("prefetch") or "").strip().lower()
    return cache in {"1", "true", "yes"} or prefetch in {"1", "true", "yes"}
