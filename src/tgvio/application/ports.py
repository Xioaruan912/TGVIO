from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from tgvio.domain.archive import (
    ArchiveCapabilities,
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
from tgvio.domain.publish import (
    PublishEffect,
    PublishPlan,
    PublishReceipt,
    PublishStep,
    PublishStepState,
)
from tgvio.domain.progress import JobProgress
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
    ) -> JobPage: ...

    async def page_failures(
        self,
        *,
        owner_id: int,
        page: int = 0,
        page_size: int = 5,
    ) -> FailurePage: ...

    async def count_by_state(self, *, owner_id: int | None = None) -> dict[JobState, int]: ...

    async def get_stats_snapshot(self, *, owner_id: int | None = None) -> dict[str, int]: ...

    async def get_recent_event_counts(
        self,
        *,
        owner_id: int | None = None,
        hours: int = 24,
    ) -> dict[str, int]: ...

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

    async def cleanup(self, *, force: bool = False) -> object: ...
