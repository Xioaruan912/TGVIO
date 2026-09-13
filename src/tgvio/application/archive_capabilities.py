from __future__ import annotations

from dataclasses import dataclass
import time

from tgvio.application.ports import JobRepository
from tgvio.domain.archive import ArchiveCapabilities


ARCHIVE_CAPABILITY_COMPONENT = "archive_capability"
ARCHIVE_PROBE_COMPONENT = "archive_probe"
ARCHIVE_CAPABILITY_STATE_VERSION = 1
ARCHIVE_CAPABILITY_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class ArchiveCapabilityStatus:
    profile_id: str
    freshness: str
    last_probe_status: str
    confirmed_at_epoch: int | None = None
    expires_at_epoch: int | None = None
    last_probe_at_epoch: int | None = None
    last_probe_error_code: str | None = None
    supports_propfind: bool | None = None
    supports_mkcol: bool | None = None
    supports_put: bool | None = None
    supports_get: bool | None = None
    supports_move: bool | None = None
    supports_etag: bool | None = None
    supports_quota: bool | None = None
    commit_mode: str | None = None


async def record_archive_probe_success(
    repository: JobRepository,
    *,
    profile_id: str,
    capabilities: ArchiveCapabilities,
    now_epoch: int | None = None,
) -> None:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    expires = now + ARCHIVE_CAPABILITY_TTL_SECONDS
    await repository.set_runtime_health(
        ARCHIVE_CAPABILITY_COMPONENT,
        "confirmed",
        detail={
            "version": ARCHIVE_CAPABILITY_STATE_VERSION,
            "profile_id": profile_id,
            "confirmed_at_epoch": now,
            "expires_at_epoch": expires,
            "supports_propfind": bool(capabilities.supports_propfind),
            "supports_mkcol": bool(capabilities.supports_mkcol),
            "supports_put": bool(capabilities.supports_put),
            "supports_get": bool(capabilities.supports_get),
            "supports_move": bool(capabilities.supports_move),
            "supports_etag": bool(capabilities.supports_etag),
            "supports_quota": bool(capabilities.supports_quota),
            "commit_mode": capabilities.commit_mode,
        },
    )
    await repository.set_runtime_health(
        ARCHIVE_PROBE_COMPONENT,
        "reachable",
        detail={
            "version": ARCHIVE_CAPABILITY_STATE_VERSION,
            "profile_id": profile_id,
            "last_probe_at_epoch": now,
        },
    )


async def record_archive_probe_failure(
    repository: JobRepository,
    *,
    profile_id: str,
    error_code: str = "probe_failed",
    now_epoch: int | None = None,
) -> None:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    await repository.set_runtime_health(
        ARCHIVE_PROBE_COMPONENT,
        "unreachable",
        detail={
            "version": ARCHIVE_CAPABILITY_STATE_VERSION,
            "profile_id": profile_id,
            "last_probe_at_epoch": now,
            "error_code": error_code,
        },
    )


async def get_archive_capability_status(
    repository: JobRepository,
    *,
    profile_id: str,
    now_epoch: int | None = None,
) -> ArchiveCapabilityStatus:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    health = await repository.get_runtime_health()
    capability_entry = health.get(ARCHIVE_CAPABILITY_COMPONENT) or {}
    capability_detail = capability_entry.get("detail")
    if not isinstance(capability_detail, dict) or capability_detail.get("profile_id") != profile_id:
        capability_detail = {}

    probe_entry = health.get(ARCHIVE_PROBE_COMPONENT) or {}
    probe_detail = probe_entry.get("detail")
    if not isinstance(probe_detail, dict) or probe_detail.get("profile_id") != profile_id:
        probe_detail = {}
        probe_status = "unknown"
    else:
        probe_status = str(probe_entry.get("status") or "unknown")

    confirmed_at = _optional_int(capability_detail.get("confirmed_at_epoch"))
    expires_at = _optional_int(capability_detail.get("expires_at_epoch"))
    if confirmed_at is None or expires_at is None:
        freshness = "unknown"
    else:
        freshness = "fresh" if now <= expires_at else "stale"

    return ArchiveCapabilityStatus(
        profile_id=profile_id,
        freshness=freshness,
        last_probe_status=probe_status,
        confirmed_at_epoch=confirmed_at,
        expires_at_epoch=expires_at,
        last_probe_at_epoch=_optional_int(probe_detail.get("last_probe_at_epoch")),
        last_probe_error_code=(
            str(probe_detail["error_code"])
            if probe_detail.get("error_code")
            else None
        ),
        supports_propfind=_optional_bool(capability_detail.get("supports_propfind")),
        supports_mkcol=_optional_bool(capability_detail.get("supports_mkcol")),
        supports_put=_optional_bool(capability_detail.get("supports_put")),
        supports_get=_optional_bool(capability_detail.get("supports_get")),
        supports_move=_optional_bool(capability_detail.get("supports_move")),
        supports_etag=_optional_bool(capability_detail.get("supports_etag")),
        supports_quota=_optional_bool(capability_detail.get("supports_quota")),
        commit_mode=(
            str(capability_detail["commit_mode"])
            if capability_detail.get("commit_mode")
            else None
        ),
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
