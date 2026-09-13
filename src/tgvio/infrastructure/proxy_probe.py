from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Callable
import urllib.parse

from tgvio.domain.diagnostics import StaticProxyState


_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "socks5": 1080,
    "socks5h": 1080,
}


@dataclass(frozen=True, slots=True)
class StaticProxyProbeResult:
    state: StaticProxyState
    checked_at_epoch: int | None


def static_proxy_unchecked_status(proxy_url: str) -> StaticProxyProbeResult:
    """Return the safe local status used by offline/check-only startup paths."""

    return StaticProxyProbeResult(
        state=(
            StaticProxyState.CONFIGURED_UNCHECKED
            if proxy_url.strip()
            else StaticProxyState.DISABLED
        ),
        checked_at_epoch=None,
    )


async def probe_static_proxy_endpoint(
    proxy_url: str,
    *,
    timeout_seconds: float = 2.0,
    now: Callable[[], float] | None = None,
) -> StaticProxyProbeResult:
    """Check only whether the configured proxy endpoint accepts a TCP connection.

    The probe never sends proxy credentials, requests an external URL, or retains the
    configured endpoint in its result.
    """

    configured = proxy_url.strip()
    if not configured:
        return StaticProxyProbeResult(
            state=StaticProxyState.DISABLED,
            checked_at_epoch=None,
        )
    try:
        parsed = urllib.parse.urlsplit(configured)
    except ValueError:
        return StaticProxyProbeResult(
            state=StaticProxyState.UNREACHABLE,
            checked_at_epoch=_now_epoch(now),
        )
    host = parsed.hostname
    if not host:
        return StaticProxyProbeResult(
            state=StaticProxyState.UNREACHABLE,
            checked_at_epoch=_now_epoch(now),
        )
    try:
        port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme.lower())
    except ValueError:
        port = None
    if port is None:
        return StaticProxyProbeResult(
            state=StaticProxyState.UNREACHABLE,
            checked_at_epoch=_now_epoch(now),
        )

    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return StaticProxyProbeResult(
            state=StaticProxyState.UNREACHABLE,
            checked_at_epoch=_now_epoch(now),
        )
    if writer is not None:
        try:
            writer.close()
            await writer.wait_closed()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The completed TCP handshake is enough to report reachability. A
            # close-side transport quirk must not turn this observability probe
            # into a startup failure or reveal endpoint details.
            pass
    return StaticProxyProbeResult(
        state=StaticProxyState.REACHABLE,
        checked_at_epoch=_now_epoch(now),
    )


def _now_epoch(now) -> int:
    return max(0, int((now or time.time)()))
