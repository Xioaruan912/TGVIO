from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DiagnosticAvailability(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"


class MigrationVerification(StrEnum):
    VERIFIED = "verified"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class LeaseFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


class StaticProxyState(StrEnum):
    DISABLED = "disabled"
    CONFIGURED_UNCHECKED = "configured_unchecked"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class SchemaDiagnostic:
    user_version: int | None
    latest_version: int | None
    ledger_contiguous: bool
    verification: MigrationVerification


@dataclass(frozen=True, slots=True)
class RuntimeLeaseDiagnostic:
    unique: bool
    generation: int | None
    freshness: LeaseFreshness


@dataclass(frozen=True, slots=True)
class SchedulerDiagnostic:
    paused: bool
    active: int
    held: int
    ready: int
    blocked: int


@dataclass(frozen=True, slots=True)
class ArchiveDiagnostic:
    planned: int
    transferring: int
    committed: int
    failed: int
    retry_wait: int
    capability_freshness: str


@dataclass(frozen=True, slots=True)
class FeatureDiagnostics:
    run_bot: bool
    publish_enabled: bool
    url_enabled: bool
    url_private_network_policy: str
    archive_enabled: bool
    archive_policy: str
    collections_enabled: bool
    auto_retry_enabled: bool
    live_fixture_enabled: bool


@dataclass(frozen=True, slots=True)
class StaticProxyDiagnostic:
    state: StaticProxyState
    checked_at_epoch: int | None


@dataclass(frozen=True, slots=True)
class CapabilitiesDiagnostic:
    """Non-sensitive environment capability booleans (no paths or versions)."""

    ffmpeg: bool
    ffprobe: bool
    yt_dlp: bool
    cryptg: bool
    hachoir: bool


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    release_id: str
    app_version: str
    commit: str
    source_manifest: str
    aggregate_status: DiagnosticAvailability
    schema: SchemaDiagnostic
    runtime_lease: RuntimeLeaseDiagnostic
    scheduler: SchedulerDiagnostic
    archive: ArchiveDiagnostic
    features: FeatureDiagnostics
    static_proxy: StaticProxyDiagnostic
    capabilities: CapabilitiesDiagnostic | None = None
