from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Awaitable, Callable, Protocol

from tgvio.application.archive_runtime import ArchiveCanonicalCacheUnavailable
from tgvio.application.job_control import JobControlService, UnsafeRetryError
from tgvio.application.ports import CacheOperator, JobRepository
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.job import Job, JobEvent, JobState
from tgvio.observability import log_event


AUTO_RECOVERY_POLICY_KEY = "auto_recovery"
JOB_RECOVERY_STATE_KEY = "auto_recovery_job"
ARCHIVE_RECOVERY_STATE_KEY = "auto_recovery_archive"

_PUBLISH_QUARANTINE_CODES = {"publish_partial", "publish_uncertain"}
_JOB_RETRY_LIMITS: dict[str, int | None] = {
    "download_failed": None,
    "disk_low": None,
    "media_analysis_failed": 1,
    "publish_failed": 2,
}
_FINAL_RECOVERY_STATUSES = {
    "abandoned",
    "exhausted",
    "manual_review",
    "quarantined",
}


class ArchiveRetryOperator(Protocol):
    async def retry_package(
        self,
        package_id: str,
        *,
        automatic: bool = False,
    ) -> ArchivePackage: ...


@dataclass(frozen=True, slots=True)
class AutoRecoveryPolicy:
    """Bounded retry parameters frozen into every newly accepted Job."""

    enabled: bool = True
    max_attempts: int = 3
    base_delay_seconds: int = 15
    max_delay_seconds: int = 300
    version: int = 1

    def __post_init__(self) -> None:
        if not 0 <= int(self.max_attempts) <= 10:
            raise ValueError("auto recovery max_attempts must be in range 0..10")
        if not 1 <= int(self.base_delay_seconds) <= 3600:
            raise ValueError("auto recovery base_delay_seconds must be in range 1..3600")
        if not int(self.base_delay_seconds) <= int(self.max_delay_seconds) <= 86400:
            raise ValueError(
                "auto recovery max_delay_seconds must be >= base delay and <= 86400"
            )

    def frozen(self) -> dict[str, object]:
        return {
            "version": int(self.version),
            "enabled": bool(self.enabled),
            "max_attempts": int(self.max_attempts),
            "base_delay_seconds": int(self.base_delay_seconds),
            "max_delay_seconds": int(self.max_delay_seconds),
        }

    @classmethod
    def from_job(cls, job: Job) -> AutoRecoveryPolicy | None:
        raw = job.policy.get(AUTO_RECOVERY_POLICY_KEY)
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return None
        if raw.get("enabled") is not True:
            return None
        try:
            policy = cls(
                enabled=True,
                max_attempts=int(raw["max_attempts"]),
                base_delay_seconds=int(raw["base_delay_seconds"]),
                max_delay_seconds=int(raw["max_delay_seconds"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return policy

    def limit_for_job_error(self, error_code: str | None) -> int | None:
        if error_code not in _JOB_RETRY_LIMITS:
            return None
        specific = _JOB_RETRY_LIMITS[error_code]
        return int(self.max_attempts) if specific is None else min(
            int(self.max_attempts),
            specific,
        )

    def delay_for_attempt(self, attempt: int) -> int:
        attempt = max(1, int(attempt))
        return min(
            int(self.max_delay_seconds),
            int(self.base_delay_seconds) * (2 ** (attempt - 1)),
        )


@dataclass(frozen=True, slots=True)
class AutoRecoveryRunResult:
    scanned_jobs: int = 0
    scheduled_jobs: int = 0
    retried_jobs: tuple[Job, ...] = ()
    exhausted_jobs: int = 0
    quarantined_jobs: int = 0
    scanned_archives: int = 0
    scheduled_archives: int = 0
    retried_archives: int = 0
    exhausted_archives: int = 0


def job_recovery_state(job: Job) -> dict[str, object]:
    value = job.policy.get(JOB_RECOVERY_STATE_KEY)
    return dict(value) if isinstance(value, dict) else {}


def archive_recovery_state(job: Job) -> dict[str, object]:
    value = job.policy.get(ARCHIVE_RECOVERY_STATE_KEY)
    return dict(value) if isinstance(value, dict) else {}


def recovery_status_is_final(status: object) -> bool:
    return str(status or "") in _FINAL_RECOVERY_STATUSES


def job_failure_waits_for_recovery(job: Job) -> bool:
    """Keep the stable status tracker alive until recovery reaches a decision."""

    if job.state != JobState.FAILED or AutoRecoveryPolicy.from_job(job) is None:
        return False
    return not recovery_status_is_final(job_recovery_state(job).get("status"))


def archive_failure_waits_for_recovery(
    job: Job,
    package: ArchivePackage | None,
) -> bool:
    if (
        package is None
        or package.state != ArchivePackageState.FAILED
        or AutoRecoveryPolicy.from_job(job) is None
    ):
        return False
    return not recovery_status_is_final(archive_recovery_state(job).get("status"))


class AutoRecoveryService:
    """Durably classify, back off and retry failures without blind side effects."""

    def __init__(
        self,
        repository: JobRepository,
        control: JobControlService,
        *,
        cache_operator: CacheOperator | None = None,
        archive_operator: ArchiveRetryOperator | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._control = control
        self._cache_operator = cache_operator
        self._archive_operator = archive_operator
        self._clock = clock
        self._log = logging.getLogger("tgvio.auto_recovery")

    async def run_once(self) -> AutoRecoveryRunResult:
        failed_jobs = await self._repository.list_failed_jobs_for_auto_recovery(limit=100)
        scheduled_jobs = 0
        retried_jobs: list[Job] = []
        exhausted_jobs = 0
        quarantined_jobs = 0
        for job in failed_jobs:
            try:
                outcome, retried = await self._recover_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "auto_recovery.job.evaluation_failed",
                    "One Job recovery evaluation failed; remaining Jobs will continue",
                    job_id=job.id,
                    error_code=job.error_code,
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )
                continue
            scheduled_jobs += outcome == "scheduled"
            exhausted_jobs += outcome in {"abandoned", "exhausted", "manual_review"}
            quarantined_jobs += outcome == "quarantined"
            if retried is not None:
                retried_jobs.append(retried)

        scanned_archives = 0
        scheduled_archives = 0
        retried_archives = 0
        exhausted_archives = 0
        if self._archive_operator is not None:
            packages = await self._repository.list_failed_archive_packages_for_auto_recovery(
                limit=100,
            )
            scanned_archives = len(packages)
            for package in packages:
                try:
                    outcome = await self._recover_archive(package)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_event(
                        self._log,
                        logging.ERROR,
                        "auto_recovery.archive.evaluation_failed",
                        "One Archive recovery evaluation failed; remaining packages will continue",
                        job_id=package.job_id,
                        package_id=package.id,
                        exception_type=type(exc).__name__,
                        exc_info=True,
                    )
                    continue
                scheduled_archives += outcome == "scheduled"
                retried_archives += outcome == "retried"
                exhausted_archives += outcome in {"abandoned", "exhausted"}

        return AutoRecoveryRunResult(
            scanned_jobs=len(failed_jobs),
            scheduled_jobs=int(scheduled_jobs),
            retried_jobs=tuple(retried_jobs),
            exhausted_jobs=int(exhausted_jobs),
            quarantined_jobs=int(quarantined_jobs),
            scanned_archives=scanned_archives,
            scheduled_archives=int(scheduled_archives),
            retried_archives=int(retried_archives),
            exhausted_archives=int(exhausted_archives),
        )

    async def _recover_job(self, job: Job) -> tuple[str | None, Job | None]:
        policy = AutoRecoveryPolicy.from_job(job)
        if policy is None:
            return None, None
        failure = await self._latest_job_failure(job.id)
        failure_id = self._job_failure_id(job, failure)
        existing = job_recovery_state(job)

        if job.error_code in _PUBLISH_QUARANTINE_CODES:
            if not self._same_decision(existing, failure_id, "quarantined"):
                await self._set_job_state(
                    job.id,
                    {
                        "status": "quarantined",
                        "failure_id": failure_id,
                        "error_code": job.error_code,
                        "decided_at_epoch": self._now(),
                        "reason": "visible_side_effect_not_safe_to_replay",
                    },
                )
                log_event(
                    self._log,
                    logging.ERROR,
                    "auto_recovery.job.quarantined",
                    "Uncertain publish was quarantined without replay; later jobs may continue",
                    job_id=job.id,
                    error_code=job.error_code,
                )
                return "quarantined", None
            return None, None

        limit = policy.limit_for_job_error(job.error_code)
        if limit is None or limit <= 0:
            if not self._same_decision(existing, failure_id, "abandoned"):
                await self._set_job_state(
                    job.id,
                    {
                        "status": "abandoned",
                        "failure_id": failure_id,
                        "error_code": job.error_code,
                        "decided_at_epoch": self._now(),
                        "reason": "error_not_retryable",
                    },
                )
                log_event(
                    self._log,
                    logging.WARNING,
                    "auto_recovery.job.abandoned",
                    "Job error is not eligible for automatic retry; later jobs may continue",
                    job_id=job.id,
                    error_code=job.error_code,
                )
                return "abandoned", None
            return None, None

        retry_count = await self._repository.get_retry_count(job.id)
        if retry_count >= limit:
            if not self._same_decision(existing, failure_id, "exhausted"):
                await self._set_job_state(
                    job.id,
                    {
                        "status": "exhausted",
                        "failure_id": failure_id,
                        "error_code": job.error_code,
                        "attempt_count": retry_count,
                        "max_attempts": limit,
                        "decided_at_epoch": self._now(),
                        "reason": "retry_budget_exhausted",
                    },
                )
                log_event(
                    self._log,
                    logging.ERROR,
                    "auto_recovery.job.exhausted",
                    "Automatic retry budget exhausted; Job remains failed and later jobs may continue",
                    job_id=job.id,
                    error_code=job.error_code,
                    retry_count=retry_count,
                    max_attempts=limit,
                )
                return "exhausted", None
            return None, None

        if existing.get("failure_id") != failure_id or existing.get("status") != "scheduled":
            attempt = retry_count + 1
            due = self._now() + policy.delay_for_attempt(attempt)
            await self._set_job_state(
                job.id,
                {
                    "status": "scheduled",
                    "failure_id": failure_id,
                    "error_code": job.error_code,
                    "next_attempt": attempt,
                    "max_attempts": limit,
                    "next_retry_epoch": due,
                },
            )
            log_event(
                self._log,
                logging.WARNING,
                "auto_recovery.job.scheduled",
                job_id=job.id,
                error_code=job.error_code,
                retry_attempt=attempt,
                max_attempts=limit,
                delay_seconds=max(0, due - self._now()),
            )
            return "scheduled", None

        try:
            due = int(existing["next_retry_epoch"])
        except (KeyError, TypeError, ValueError):
            due = self._now()
        if due > self._now():
            return None, None

        current = await self._repository.get(job.id)
        if current is None or current.state != JobState.FAILED:
            return None, None
        latest = await self._latest_job_failure(current.id)
        if self._job_failure_id(current, latest) != failure_id:
            return None, None

        if current.error_code == "disk_low" and self._cache_operator is not None:
            try:
                result = await self._cache_operator.cleanup(force=True)
                log_event(
                    self._log,
                    logging.INFO,
                    "auto_recovery.disk_cleanup.completed",
                    job_id=current.id,
                    removed_jobs=getattr(result, "removed_jobs", 0),
                    removed_bytes=getattr(result, "removed_bytes", 0),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.WARNING,
                    "auto_recovery.disk_cleanup.failed",
                    job_id=current.id,
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )

        try:
            decision = await self._control.retry_failed(current)
        except UnsafeRetryError as exc:
            await self._set_job_state(
                current.id,
                {
                    "status": "manual_review",
                    "failure_id": failure_id,
                    "error_code": current.error_code,
                    "attempt_count": retry_count,
                    "max_attempts": limit,
                    "decided_at_epoch": self._now(),
                    "reason": "retry_safety_check_failed",
                },
            )
            log_event(
                self._log,
                logging.ERROR,
                "auto_recovery.job.manual_review",
                "Automatic retry stopped by side-effect safety check",
                job_id=current.id,
                error_code=current.error_code,
                exception_type=type(exc).__name__,
            )
            return "manual_review", None
        except ValueError:
            # A user control or another worker won the race after the read.
            return None, None

        retried = await self._set_job_state(
            current.id,
            {
                "status": "retrying",
                "failure_id": failure_id,
                "error_code": current.error_code,
                "attempt_count": decision.retry_count,
                "max_attempts": limit,
                "last_retry_epoch": self._now(),
            },
        )
        log_event(
            self._log,
            logging.WARNING,
            "auto_recovery.job.retried",
            job_id=current.id,
            error_code=current.error_code,
            retry_attempt=decision.retry_count,
            max_attempts=limit,
            target_state=decision.target_state.value,
        )
        return "retried", retried

    async def _recover_archive(self, package: ArchivePackage) -> str | None:
        job = await self._repository.get(package.job_id)
        if job is None:
            return None
        policy = AutoRecoveryPolicy.from_job(job)
        if policy is None:
            return None
        events = await self._repository.list_archive_events(package.id)
        failure_event = next(
            (event for event in reversed(events) if event.event_type == "archive_failed"),
            events[-1] if events else None,
        )
        failure_id = (
            f"event:{failure_event.id}"
            if failure_event is not None and failure_event.id is not None
            else f"package:{package.updated_at or package.id}:{package.error_code or 'unknown'}"
        )
        existing = archive_recovery_state(job)
        try:
            attempt_count = max(0, int(existing.get("attempt_count", 0)))
        except (TypeError, ValueError):
            attempt_count = 0
        limit = int(policy.max_attempts)
        if attempt_count >= limit:
            if not self._same_decision(existing, failure_id, "exhausted"):
                await self._set_archive_state(
                    job.id,
                    {
                        "status": "exhausted",
                        "failure_id": failure_id,
                        "package_id": package.id,
                        "attempt_count": attempt_count,
                        "max_attempts": limit,
                        "decided_at_epoch": self._now(),
                        "reason": "retry_budget_exhausted",
                    },
                )
                log_event(
                    self._log,
                    logging.ERROR,
                    "auto_recovery.archive.exhausted",
                    "Archive automatic retry budget exhausted; Telegram result is unchanged",
                    job_id=job.id,
                    package_id=package.id,
                    retry_count=attempt_count,
                    max_attempts=limit,
                )
                return "exhausted"
            return None

        if existing.get("failure_id") != failure_id or existing.get("status") != "scheduled":
            attempt = attempt_count + 1
            due = self._now() + policy.delay_for_attempt(attempt)
            await self._set_archive_state(
                job.id,
                {
                    "status": "scheduled",
                    "failure_id": failure_id,
                    "package_id": package.id,
                    "attempt_count": attempt_count,
                    "next_attempt": attempt,
                    "max_attempts": limit,
                    "next_retry_epoch": due,
                },
            )
            log_event(
                self._log,
                logging.WARNING,
                "auto_recovery.archive.scheduled",
                job_id=job.id,
                package_id=package.id,
                retry_attempt=attempt,
                max_attempts=limit,
                delay_seconds=max(0, due - self._now()),
            )
            return "scheduled"

        try:
            due = int(existing["next_retry_epoch"])
        except (KeyError, TypeError, ValueError):
            due = self._now()
        if due > self._now():
            return None

        current = await self._repository.get_archive_package(package.id)
        if current is None or current.state != ArchivePackageState.FAILED:
            return None
        attempt = attempt_count + 1
        await self._set_archive_state(
            job.id,
            {
                **existing,
                "status": "retrying",
                "attempt_count": attempt,
                "max_attempts": limit,
                "last_retry_epoch": self._now(),
            },
        )
        try:
            assert self._archive_operator is not None
            await self._archive_operator.retry_package(current.id, automatic=True)
        except asyncio.CancelledError:
            raise
        except ArchiveCanonicalCacheUnavailable as exc:
            await self._set_archive_state(
                job.id,
                {
                    **existing,
                    "status": "abandoned",
                    "failure_id": failure_id,
                    "package_id": package.id,
                    "attempt_count": attempt,
                    "max_attempts": limit,
                    "decided_at_epoch": self._now(),
                    "reason": "canonical_cache_unavailable",
                },
            )
            log_event(
                self._log,
                logging.ERROR,
                "auto_recovery.archive.abandoned",
                "Archive cannot retry because canonical cache is unavailable",
                job_id=job.id,
                package_id=package.id,
                exception_type=type(exc).__name__,
            )
            return "abandoned"
        except Exception as exc:
            if attempt >= limit:
                status = "exhausted"
                due = None
            else:
                status = "scheduled"
                due = self._now() + policy.delay_for_attempt(attempt + 1)
            state = {
                **existing,
                "status": status,
                "failure_id": failure_id,
                "package_id": package.id,
                "attempt_count": attempt,
                "max_attempts": limit,
                "reason": "archive_retry_request_failed",
            }
            if due is not None:
                state["next_retry_epoch"] = due
                state["next_attempt"] = attempt + 1
            else:
                state["decided_at_epoch"] = self._now()
            await self._set_archive_state(job.id, state)
            log_event(
                self._log,
                logging.ERROR,
                "auto_recovery.archive.retry_request_failed",
                job_id=job.id,
                package_id=package.id,
                retry_attempt=attempt,
                max_attempts=limit,
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            return status

        log_event(
            self._log,
            logging.WARNING,
            "auto_recovery.archive.retried",
            job_id=job.id,
            package_id=package.id,
            retry_attempt=attempt,
            max_attempts=limit,
        )
        return "retried"

    async def _latest_job_failure(self, job_id: str) -> JobEvent | None:
        events = await self._repository.list_events(job_id)
        return next(
            (
                event
                for event in reversed(events)
                if event.to_state == JobState.FAILED
                and event.from_state != JobState.FAILED
            ),
            None,
        )

    @staticmethod
    def _job_failure_id(job: Job, event: JobEvent | None) -> str:
        if event is not None and event.id is not None:
            return f"event:{event.id}"
        return f"job:{job.updated_at or job.created_at or job.id}:{job.error_code or 'unknown'}"

    async def _set_job_state(self, job_id: str, state: dict[str, object]) -> Job:
        return await self._repository.update_job_policy(
            job_id,
            {JOB_RECOVERY_STATE_KEY: state},
        )

    async def _set_archive_state(self, job_id: str, state: dict[str, object]) -> Job:
        return await self._repository.update_job_policy(
            job_id,
            {ARCHIVE_RECOVERY_STATE_KEY: state},
        )

    @staticmethod
    def _same_decision(
        existing: dict[str, object],
        failure_id: str,
        status: str,
    ) -> bool:
        return existing.get("failure_id") == failure_id and existing.get("status") == status

    def _now(self) -> int:
        return int(self._clock())


class AutoRecoveryRuntime:
    """Wakeable retry loop that dispatches safely reset Jobs back to the runtime."""

    def __init__(
        self,
        service: AutoRecoveryService,
        schedule_job: Callable[[Job], Awaitable[None]],
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        self._service = service
        self._schedule_job = schedule_job
        self._poll_seconds = max(0.25, float(poll_seconds))
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._log = logging.getLogger("tgvio.auto_recovery.runtime")

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="tgvio-auto-recovery")
            self._wake.set()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def notify(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                result = await self._service.run_once()
                for job in result.retried_jobs:
                    try:
                        await self._schedule_job(job)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # The Job is already durable and non-terminal. Startup
                        # recovery will dispatch it after a process restart.
                        log_event(
                            self._log,
                            logging.ERROR,
                            "auto_recovery.job.dispatch_failed",
                            "Retry was persisted but local dispatch failed",
                            job_id=job.id,
                            exception_type=type(exc).__name__,
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "auto_recovery.runtime.iteration_failed",
                    "Automatic recovery iteration failed",
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass
