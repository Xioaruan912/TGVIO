from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from tgvio.domain.archive import (
    ArchiveCapabilities,
    ArchiveDeleteReceipt,
    ArchiveDeletion,
    ArchiveDeletionEvent,
    ArchiveDeletionTarget,
    ArchiveDeletionTargetState,
    ArchiveEvent,
    ArchiveObject,
    ArchiveObjectState,
    ArchivePackage,
    ArchivePackageState,
    ArchivePlan,
    ArchiveRemoteStat,
    ArchiveStoreReceipt,
)
from tgvio.domain.control import JobControlState, QueueControlState
from tgvio.domain.intake import (
    CollectionEntry,
    CollectionSession,
    IntakeEventKey,
    JobDisplayMessage,
    SpoilerMode,
    UserPreference,
)
from tgvio.domain.job import Job, JobEvent, JobState, MediaItem, MediaKind
from tgvio.domain.job_query import FailurePage, JobListFilter, JobPage
from tgvio.domain.operations import OperationToken, PublishEffectRevocation, RevocationState
from tgvio.domain.publish import (
    PublishEffect,
    PublishPlan,
    PublishReceipt,
    PublishStep,
    PublishStepState,
)
from tgvio.domain.progress import JobProgress
from tgvio.domain.notifications import NotificationEvent, OutboxEntry
from tgvio.domain.scheduler import PhaseClaim, PublishGate, RuntimeLease


TransferProgressCallback = Callable[[int, int | None], None]


class OperationalLogReader(Protocol):
    async def recent_for_job(
        self,
        job_id: str,
        *,
        plan_id: str | None = None,
        package_id: str | None = None,
        limit: int = 40,
    ) -> list[dict[str, object]]: ...


class PublishTransportPartialError(RuntimeError):
    """Transport failed after one or more externally visible side effects."""

    def __init__(self, message: str, receipts: tuple[PublishReceipt, ...]) -> None:
        super().__init__(message)
        self.receipts = receipts


class PublishTransportUncertainError(RuntimeError):
    """A visible Telegram send may have succeeded but no receipt was obtained."""


class JobRepository(Protocol):
    async def create(self, job: Job) -> None: ...

    async def lookup_intake_events(
        self,
        keys: tuple[IntakeEventKey, ...],
    ) -> dict[IntakeEventKey, str]: ...

    async def create_with_intake_events(
        self,
        job: Job,
        events: tuple[tuple[IntakeEventKey, int], ...],
    ) -> bool: ...

    async def get_open_collection(
        self,
        owner_id: int,
        chat_id: int,
    ) -> CollectionSession | None: ...

    async def get_collection(self, session_id: str) -> CollectionSession | None: ...

    async def create_collection(self, session: CollectionSession) -> CollectionSession: ...

    async def set_collection_status_message(
        self,
        session_id: str,
        chat_id: int,
        message_id: int,
    ) -> CollectionSession: ...

    async def append_collection_entries(
        self,
        session_id: str,
        entries: tuple[CollectionEntry, ...],
    ) -> list[CollectionEntry]: ...

    async def list_collection_entries(self, session_id: str) -> list[CollectionEntry]: ...

    async def finalize_collection(
        self,
        session_id: str,
        job_ids: tuple[str, ...],
    ) -> CollectionSession: ...

    async def cancel_collection(self, session_id: str) -> CollectionSession: ...

    async def get_user_preference(self, owner_id: int) -> UserPreference: ...

    async def set_user_spoiler_mode(self, owner_id: int, mode: SpoilerMode) -> UserPreference: ...

    async def get_job_display_message(self, job_id: str) -> JobDisplayMessage | None: ...

    async def save_job_display_message(self, ref: JobDisplayMessage) -> JobDisplayMessage: ...

    async def save(self, job: Job) -> None: ...

    async def update_job_policy(
        self,
        job_id: str,
        updates: dict[str, object],
        *,
        remove_keys: tuple[str, ...] = (),
    ) -> Job: ...

    async def get(self, job_id: str) -> Job | None: ...

    async def list_items(self, job_id: str) -> list[MediaItem]: ...

    async def list_events(self, job_id: str) -> list[JobEvent]: ...

    async def list_by_states(self, states: tuple[JobState, ...]) -> list[Job]: ...

    async def list_failed_jobs_for_auto_recovery(self, *, limit: int = 100) -> list[Job]: ...

    async def list_recent(self, *, owner_id: int | None = None, limit: int = 10) -> list[Job]: ...

    async def page_jobs(
        self,
        *,
        owner_id: int,
        filter: JobListFilter = JobListFilter.ALL,
        page: int = 0,
        page_size: int = 5,
        business_day_start_epoch: float | None = None,
    ) -> JobPage: ...

    async def page_failures(
        self,
        *,
        owner_id: int,
        page: int = 0,
        page_size: int = 5,
    ) -> FailurePage: ...

    async def count_by_state(self, *, owner_id: int | None = None) -> dict[JobState, int]: ...

    async def get_stats_snapshot(
        self,
        *,
        owner_id: int | None = None,
        business_day: str | None = None,
    ) -> dict[str, int]: ...

    async def get_display_no(self, job_id: str) -> int | None: ...

    async def get_display_identity(self, job_id: str) -> dict[str, object] | None: ...

    async def display_numbers_for(self, job_ids: tuple[str, ...]) -> dict[str, int]: ...

    async def get_by_display_number(
        self,
        owner_id: int,
        business_day: str,
        display_no: int,
    ) -> Job | None: ...

    async def list_terminal_job_ids_before(
        self,
        *,
        cutoff_epoch: float,
        limit: int = 200,
        offset: int = 0,
    ) -> list[str]: ...

    async def hide_jobs(
        self,
        job_ids: tuple[str, ...],
        *,
        reason: str,
        now: float,
    ) -> int: ...

    async def job_has_active_claim(self, job_id: str, *, now: float) -> bool: ...

    async def job_has_unsettled_publish_step(self, job_id: str) -> bool: ...

    async def job_has_pending_revocation(self, job_id: str) -> bool: ...

    async def get_maintenance_run(
        self,
        *,
        kind: str,
        business_day: str,
    ) -> dict[str, object] | None: ...

    async def ensure_maintenance_run(
        self,
        *,
        kind: str,
        business_day: str,
        cutoff_at: float,
        now: float,
    ) -> dict[str, object]: ...

    async def has_maintenance_targets(self, run_id: int) -> bool: ...

    async def claim_maintenance_run(
        self,
        run_id: int,
        *,
        holder_id: str,
        now: float,
        lease_seconds: float = 120.0,
    ) -> dict[str, object] | None: ...

    async def finish_maintenance_run(
        self,
        run_id: int,
        *,
        status: str,
        now: float,
        error: str | None = None,
        next_retry_at: float | None = None,
        attempt: int | None = None,
    ) -> None: ...

    async def upsert_maintenance_targets(
        self,
        run_id: int,
        targets: tuple[dict[str, object], ...],
        *,
        now: float,
    ) -> int: ...

    async def list_unfinished_maintenance_targets(
        self, run_id: int
    ) -> list[dict[str, object]]: ...

    async def set_maintenance_target_status(
        self,
        target_id: int,
        *,
        status: str,
        now: float,
        phase: str | None = None,
    ) -> None: ...

    async def recycle_operation_tokens(
        self,
        *,
        now: float,
        consumed_grace_seconds: float = 86400.0,
    ) -> int: ...

    async def prune_settled_outbox(self, *, cutoff_epoch: float) -> int: ...

    async def get_recent_event_counts(
        self,
        *,
        owner_id: int | None = None,
        hours: int = 24,
    ) -> dict[str, int]: ...

    async def get_diagnostic_aggregates(self) -> dict[str, object]: ...

    async def get_admin_job_page(
        self,
        *,
        states: tuple[str, ...] = (),
        page: int = 0,
        page_size: int = 20,
    ) -> dict[str, object]: ...

    async def enqueue_notification(self, event: NotificationEvent, *, now: float) -> bool: ...

    async def claim_due_notifications(
        self,
        *,
        now: float,
        holder_id: str,
        limit: int = 10,
        lease_seconds: float = 120.0,
    ) -> list[OutboxEntry]: ...

    async def complete_notification(
        self,
        notification_id: int,
        *,
        holder_id: str,
        now: float,
    ) -> bool: ...

    async def fail_notification(
        self,
        notification_id: int,
        *,
        holder_id: str,
        error_code: str,
        now: float,
        base_seconds: float = 30.0,
        cap_seconds: float = 3600.0,
        jitter: float = 0.0,
    ) -> OutboxEntry | None: ...

    async def count_notification_outbox(self) -> dict[str, int]: ...

    async def get_notification_candidates(
        self,
        *,
        since_epoch: int,
        limit: int = 100,
    ) -> list[dict[str, object]]: ...

    async def next_archive_day_seq(self, day: str) -> int: ...

    async def get_runtime_flags(self) -> dict[str, str]: ...

    async def set_runtime_flag(self, key: str, value: str) -> None: ...

    async def quick_check(self) -> bool: ...

    async def set_job_progress(self, progress: JobProgress) -> None: ...

    async def get_job_progress(self, job_id: str) -> JobProgress | None: ...

    async def save_publish_plan(self, plan: PublishPlan) -> None: ...

    async def get_publish_plan(self, job_id: str) -> PublishPlan | None: ...

    async def update_publish_step_state(
        self,
        plan_id: str,
        step_index: int,
        state: PublishStepState,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> PublishStep: ...

    async def record_publish_effect(self, effect: PublishEffect) -> PublishEffect: ...

    async def record_publish_effects(
        self,
        effects: tuple[PublishEffect, ...],
    ) -> list[PublishEffect]: ...

    async def list_publish_effects(self, plan_id: str) -> list[PublishEffect]: ...

    async def create_operation_token(
        self,
        *,
        token: str,
        owner_id: int,
        action: str,
        resource_type: str,
        resource_id: str,
        expected_revision: int,
        payload_hash: str,
        payload: dict[str, object],
        ttl_seconds: int,
    ) -> OperationToken: ...

    async def get_operation_token(self, token: str) -> OperationToken | None: ...

    async def consume_operation_token(
        self,
        *,
        token: str,
        owner_id: int,
        action: str,
        resource_type: str,
        resource_id: str,
        expected_revision: int,
        payload_hash: str,
    ) -> OperationToken | None: ...

    async def ensure_publish_effect_revocations(
        self,
        job_id: str,
        effect_ids: tuple[int, ...],
    ) -> None: ...

    async def list_publish_effect_revocations(
        self,
        job_id: str,
    ) -> list[PublishEffectRevocation]: ...

    async def checkpoint_publish_effect_revocations(
        self,
        job_id: str,
        effect_ids: tuple[int, ...],
        *,
        state: RevocationState,
        error_code: str | None = None,
    ) -> list[PublishEffectRevocation]: ...

    async def list_publish_effect_revocation_events(
        self,
        job_id: str,
    ) -> list[dict[str, object]]: ...

    async def get_telegram_reference(
        self,
        sha256: str,
        destination: str,
        media_kind: MediaKind,
    ) -> str | None: ...

    async def upsert_telegram_reference(
        self,
        sha256: str,
        destination: str,
        media_kind: MediaKind,
        reference: str,
    ) -> None: ...

    async def set_runtime_health(
        self,
        component: str,
        status: str,
        *,
        detail: dict[str, object] | None = None,
    ) -> None: ...

    async def get_runtime_health(self) -> dict[str, dict[str, object]]: ...

    async def request_cancel(self, job_id: str, *, reason: str | None = None) -> None: ...

    async def clear_cancel_request(self, job_id: str) -> None: ...

    async def is_cancel_requested(self, job_id: str) -> bool: ...

    async def request_hold(self, job_id: str, *, reason: str | None = None) -> JobControlState: ...

    async def clear_hold(self, job_id: str) -> JobControlState: ...

    async def get_job_control(self, job_id: str) -> JobControlState: ...

    async def set_queue_paused(
        self,
        paused: bool,
        *,
        reason: str | None = None,
    ) -> QueueControlState: ...

    async def get_queue_control(self) -> QueueControlState: ...

    async def increment_retry_count(self, job_id: str) -> int: ...

    async def get_retry_count(self, job_id: str) -> int: ...

    async def save_archive_plan(self, plan: ArchivePlan) -> ArchivePackage: ...

    async def get_archive_package(self, package_id: str) -> ArchivePackage | None: ...

    async def get_archive_package_for_job(self, job_id: str) -> ArchivePackage | None: ...

    async def list_archive_packages_by_states(
        self,
        states: tuple[ArchivePackageState, ...],
        *,
        limit: int = 20,
    ) -> list[ArchivePackage]: ...

    async def list_failed_archive_packages_for_auto_recovery(
        self,
        *,
        limit: int = 100,
    ) -> list[ArchivePackage]: ...

    async def count_archive_packages_by_state(
        self,
        *,
        owner_id: int | None = None,
    ) -> dict[ArchivePackageState, int]: ...

    async def list_recent_archive_packages(
        self,
        *,
        owner_id: int | None = None,
        limit: int = 5,
    ) -> list[ArchivePackage]: ...

    async def list_archive_events(self, package_id: str) -> list[ArchiveEvent]: ...

    async def ensure_archive_deletion(
        self,
        package_id: str,
        *,
        target_set_hash: str,
        targets: tuple[ArchiveDeletionTarget, ...],
    ) -> ArchiveDeletion: ...

    async def get_archive_deletion(self, package_id: str) -> ArchiveDeletion | None: ...

    async def begin_archive_deletion(
        self,
        package_id: str,
        *,
        expected_revision: int,
    ) -> ArchiveDeletion: ...

    async def begin_archive_deletion_target(
        self,
        package_id: str,
        target_id: int,
    ) -> ArchiveDeletionTarget: ...

    async def checkpoint_archive_deletion_target(
        self,
        package_id: str,
        target_id: int,
        *,
        state: ArchiveDeletionTargetState,
        error_code: str | None = None,
        verification_method: str | None = None,
        already_missing: bool = False,
    ) -> ArchiveDeletion: ...

    async def finalize_archive_deletion(self, package_id: str) -> ArchiveDeletion: ...

    async def list_archive_deletion_events(
        self,
        package_id: str,
    ) -> list[ArchiveDeletionEvent]: ...

    async def update_archive_package_state(
        self,
        package_id: str,
        state: ArchivePackageState,
        *,
        event_type: str,
        detail: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        committed: bool = False,
    ) -> ArchivePackage: ...

    async def update_archive_object_state(
        self,
        object_id: int,
        state: ArchiveObjectState,
        *,
        event_type: str,
        verification_method: str | None = None,
        remote_etag: str | None = None,
        retry_count: int | None = None,
        next_retry_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> ArchiveObject: ...

    async def transition(
        self,
        job_id: str,
        next_state: JobState,
        *,
        event_type: str,
        detail: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Job: ...

    async def acquire_runtime_lease(
        self,
        lease_name: str,
        holder_id: str,
        *,
        ttl_seconds: int,
    ) -> RuntimeLease | None: ...

    async def heartbeat_runtime_lease(
        self,
        lease: RuntimeLease,
        *,
        ttl_seconds: int,
    ) -> RuntimeLease | None: ...

    async def release_runtime_lease(self, lease: RuntimeLease) -> bool: ...

    async def acquire_phase_claim(
        self,
        job_id: str,
        phase: str,
        holder_id: str,
        *,
        ttl_seconds: int,
    ) -> PhaseClaim | None: ...

    async def heartbeat_phase_claim(
        self,
        claim: PhaseClaim,
        *,
        ttl_seconds: int,
    ) -> PhaseClaim | None: ...

    async def release_phase_claim(self, claim: PhaseClaim) -> bool: ...

    async def get_accepted_order(self, job_id: str) -> int | None: ...

    async def get_by_accepted_order(self, owner_id: int, accepted_order: int) -> Job | None: ...

    async def get_next_publish_gate(self) -> PublishGate | None: ...


class TelegramGateway(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class Publisher(Protocol):
    async def publish(self, job: Job) -> None: ...


class MediaInspector(Protocol):
    async def inspect(self, item: MediaItem) -> MediaItem: ...


class MediaDownloader(Protocol):
    async def download(
        self,
        item: MediaItem,
        target_dir: Path,
        progress_callback: TransferProgressCallback | None = None,
    ) -> MediaItem: ...


class PublishedMessageRemover(Protocol):
    async def delete_message(self, peer_id: int, message_id: int) -> None: ...


class PublishTransport(Protocol):
    async def execute_step(
        self,
        job: Job,
        step: PublishStep,
        prior_effects: tuple[PublishEffect, ...],
    ) -> list[PublishReceipt]: ...


class ArchiveTransport(Protocol):
    async def probe(self) -> ArchiveCapabilities: ...

    async def ensure_collection(self, remote_path: str) -> None: ...

    async def stat(self, remote_path: str) -> ArchiveRemoteStat: ...

    async def put_file(
        self,
        local_path: Path,
        remote_path: str,
        expected_size: int,
    ) -> ArchiveStoreReceipt: ...

    async def put_bytes(
        self,
        payload: bytes,
        remote_path: str,
        *,
        content_type: str,
    ) -> ArchiveStoreReceipt: ...

    async def get_bytes(self, remote_path: str, *, max_bytes: int) -> bytes | None: ...

    async def move_collection(self, source_path: str, destination_path: str) -> None: ...

    async def delete_file(
        self,
        remote_path: str,
        *,
        expected_size: int,
        expected_etag: str | None = None,
        expected_sha256: str | None = None,
    ) -> ArchiveDeleteReceipt: ...


class ArchiveEnqueuer(Protocol):
    async def enqueue_job(self, job: Job) -> ArchivePackage | None: ...


class ArchiveOperator(ArchiveEnqueuer, Protocol):
    async def retry_package(
        self,
        package_id: str,
        *,
        automatic: bool = False,
    ) -> ArchivePackage: ...

    async def probe(self) -> ArchiveCapabilities: ...


class CacheOperator(Protocol):
    async def stats(self) -> object: ...

    async def cleanup_candidates(self, *, force: bool = False) -> tuple[str, ...]: ...

    async def cleanup(
        self,
        *,
        force: bool = False,
        job_ids: tuple[str, ...] | None = None,
    ) -> object: ...
