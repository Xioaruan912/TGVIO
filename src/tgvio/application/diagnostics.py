from __future__ import annotations

from dataclasses import dataclass
import os
import re
import time
from typing import Callable

from tgvio import __version__
from tgvio.application.archive_capabilities import get_archive_capability_status
from tgvio.application.ports import JobRepository
from tgvio.domain.diagnostics import (
    ArchiveDiagnostic,
    CapabilitiesDiagnostic,
    DiagnosticAvailability,
    DiagnosticSnapshot,
    FeatureDiagnostics,
    LeaseFreshness,
    MigrationVerification,
    RuntimeLeaseDiagnostic,
    SchedulerDiagnostic,
    SchemaDiagnostic,
    StaticProxyDiagnostic,
    StaticProxyState,
)


STATIC_PROXY_HEALTH_COMPONENT = "static_proxy"
STATIC_PROXY_HEALTH_VERSION = 1

_SAFE_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}")
_SAFE_ENUM = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MAX_DIAGNOSTIC_EPOCH = 4_102_444_800  # 2100-01-01 UTC


@dataclass(frozen=True, slots=True)
class DiagnosticFeatureConfig:
    run_bot: bool
    publish_enabled: bool
    url_enabled: bool
    url_private_network_policy: str
    archive_enabled: bool
    archive_profile_id: str
    archive_policy: str
    collections_enabled: bool
    auto_retry_enabled: bool
    live_fixture_enabled: bool
    static_proxy_configured: bool


class DiagnosticSnapshotService:
    """Build a fixed, local-only, redacted operational diagnostic projection."""

    def __init__(
        self,
        repository: JobRepository,
        *,
        features: DiagnosticFeatureConfig,
        schema_status: Callable[[], dict[str, object]],
        now: Callable[[], int] | None = None,
        capabilities: Callable[[], CapabilitiesDiagnostic] | None = None,
    ) -> None:
        self._repository = repository
        self._features = features
        self._schema_status = schema_status
        self._now = now or (lambda: int(time.time()))
        self._capabilities = capabilities

    async def snapshot(self) -> DiagnosticSnapshot:
        aggregate_status = DiagnosticAvailability.READY
        try:
            aggregates = await self._repository.get_diagnostic_aggregates()
        except Exception:
            aggregate_status = DiagnosticAvailability.UNAVAILABLE
            aggregates = {}

        try:
            runtime_health = await self._repository.get_runtime_health()
        except Exception:
            runtime_health = {}

        schema = self._schema_projection()
        lease = self._lease_projection(aggregates)
        scheduler = SchedulerDiagnostic(
            paused=_bool_value(aggregates.get("queue_paused")),
            active=_nonnegative_int(aggregates.get("scheduler_active")),
            held=_nonnegative_int(aggregates.get("scheduler_held")),
            ready=_nonnegative_int(aggregates.get("scheduler_ready")),
            blocked=_nonnegative_int(aggregates.get("scheduler_blocked")),
        )

        if self._features.archive_enabled:
            try:
                capability = await get_archive_capability_status(
                    self._repository,
                    profile_id=self._features.archive_profile_id,
                    now_epoch=self._now(),
                )
                capability_freshness = _safe_enum(capability.freshness, fallback="unknown")
            except Exception:
                capability_freshness = "unknown"
        else:
            capability_freshness = "disabled"

        archive = ArchiveDiagnostic(
            planned=_nonnegative_int(aggregates.get("archive_planned")),
            transferring=_nonnegative_int(aggregates.get("archive_transferring")),
            committed=_nonnegative_int(aggregates.get("archive_committed")),
            failed=_nonnegative_int(aggregates.get("archive_failed")),
            retry_wait=_nonnegative_int(aggregates.get("archive_retry_wait")),
            capability_freshness=capability_freshness,
        )
        proxy = self._proxy_projection(runtime_health)
        features = FeatureDiagnostics(
            run_bot=bool(self._features.run_bot),
            publish_enabled=bool(self._features.publish_enabled),
            url_enabled=bool(self._features.url_enabled),
            url_private_network_policy=_safe_enum(
                self._features.url_private_network_policy,
                fallback="unknown",
            ),
            archive_enabled=bool(self._features.archive_enabled),
            archive_policy=_safe_enum(self._features.archive_policy, fallback="unknown"),
            collections_enabled=bool(self._features.collections_enabled),
            auto_retry_enabled=bool(self._features.auto_retry_enabled),
            live_fixture_enabled=bool(self._features.live_fixture_enabled),
        )
        return DiagnosticSnapshot(
            release_id=_safe_release_identity(os.getenv("RELEASE_ID"), kind="release"),
            app_version=_safe_release_identity(__version__, kind="version"),
            commit=_safe_release_identity(os.getenv("APP_COMMIT"), kind="commit"),
            source_manifest=_safe_release_identity(
                os.getenv("SOURCE_MANIFEST"),
                kind="source_manifest",
            ),
            aggregate_status=aggregate_status,
            schema=schema,
            runtime_lease=lease,
            scheduler=scheduler,
            archive=archive,
            features=features,
            static_proxy=proxy,
            capabilities=self._capability_projection(),
        )

    def _capability_projection(self) -> CapabilitiesDiagnostic | None:
        if self._capabilities is None:
            return None
        try:
            return self._capabilities()
        except Exception:
            return None

    def _schema_projection(self) -> SchemaDiagnostic:
        try:
            status = self._schema_status()
        except Exception:
            return SchemaDiagnostic(
                user_version=None,
                latest_version=None,
                ledger_contiguous=False,
                verification=MigrationVerification.UNAVAILABLE,
            )
        user_version = _optional_nonnegative_int(status.get("user_version"))
        latest_version = _optional_nonnegative_int(status.get("latest_version"))
        raw_versions = status.get("recorded_versions")
        versions = (
            [value for value in raw_versions if isinstance(value, int) and not isinstance(value, bool)]
            if isinstance(raw_versions, list)
            else []
        )
        expected = (
            list(range(1, latest_version + 1))
            if latest_version is not None and latest_version > 0
            else []
        )
        ledger_present = status.get("ledger_present") is True
        contiguous = bool(ledger_present and versions == expected)
        verified = bool(
            contiguous
            and user_version is not None
            and latest_version is not None
            and user_version == latest_version
        )
        return SchemaDiagnostic(
            user_version=user_version,
            latest_version=latest_version,
            ledger_contiguous=contiguous,
            verification=(
                MigrationVerification.VERIFIED
                if verified
                else MigrationVerification.INVALID
            ),
        )

    @staticmethod
    def _lease_projection(aggregates: dict[str, object]) -> RuntimeLeaseDiagnostic:
        active_count = _nonnegative_int(aggregates.get("runtime_lease_active_count"))
        generation = _optional_nonnegative_int(aggregates.get("runtime_lease_generation"))
        expires_at = _optional_nonnegative_int(aggregates.get("runtime_lease_expires_at"))
        now_epoch = _optional_nonnegative_int(aggregates.get("database_now_epoch"))
        if generation is None:
            freshness = LeaseFreshness.MISSING
        elif expires_at is None or now_epoch is None:
            freshness = LeaseFreshness.UNAVAILABLE
        elif expires_at > now_epoch:
            freshness = LeaseFreshness.FRESH
        else:
            freshness = LeaseFreshness.STALE
        return RuntimeLeaseDiagnostic(
            unique=(active_count == 1 and freshness == LeaseFreshness.FRESH),
            generation=generation,
            freshness=freshness,
        )

    def _proxy_projection(
        self,
        runtime_health: dict[str, dict[str, object]],
    ) -> StaticProxyDiagnostic:
        if not self._features.static_proxy_configured:
            return StaticProxyDiagnostic(
                state=StaticProxyState.DISABLED,
                checked_at_epoch=None,
            )
        entry = runtime_health.get(STATIC_PROXY_HEALTH_COMPONENT)
        if not isinstance(entry, dict):
            return StaticProxyDiagnostic(
                state=StaticProxyState.CONFIGURED_UNCHECKED,
                checked_at_epoch=None,
            )
        raw_state = entry.get("status")
        try:
            state = StaticProxyState(str(raw_state))
        except ValueError:
            state = StaticProxyState.CONFIGURED_UNCHECKED
        if state == StaticProxyState.DISABLED:
            state = StaticProxyState.CONFIGURED_UNCHECKED
        detail = entry.get("detail")
        checked_at = (
            _optional_diagnostic_epoch(detail.get("checked_at_epoch"))
            if isinstance(detail, dict)
            else None
        )
        return StaticProxyDiagnostic(state=state, checked_at_epoch=checked_at)


def static_proxy_health_detail(*, checked_at_epoch: int | None) -> dict[str, object]:
    detail: dict[str, object] = {"version": STATIC_PROXY_HEALTH_VERSION}
    if checked_at_epoch is not None:
        detail["checked_at_epoch"] = max(0, int(checked_at_epoch))
    return detail


def _safe_release_identity(value: str | None, *, kind: str) -> str:
    candidate = (value or "").strip()
    if kind == "commit":
        return candidate if _HEX40.fullmatch(candidate) else "unknown"
    if kind == "source_manifest":
        return candidate if _HEX64.fullmatch(candidate) else "unknown"
    if kind == "version":
        return candidate if _SAFE_VERSION.fullmatch(candidate) else "unknown"
    return candidate if _SAFE_RELEASE_ID.fullmatch(candidate) else "unknown"


def _safe_enum(value: object, *, fallback: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _SAFE_ENUM.fullmatch(candidate) else fallback


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return False


def _nonnegative_int(value: object) -> int:
    parsed = _optional_nonnegative_int(value)
    return 0 if parsed is None else parsed


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _optional_diagnostic_epoch(value: object) -> int | None:
    epoch = _optional_nonnegative_int(value)
    if epoch is None or epoch > _MAX_DIAGNOSTIC_EPOCH:
        return None
    return epoch
