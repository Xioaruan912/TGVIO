from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from tgvio.application.archive_executor import ArchiveExecutor
from tgvio.application.archive_planner import ArchivePlanner
from tgvio.application.ports import ArchiveTransport, JobRepository
from tgvio.application.scheduler import PhaseClaimGuard, PhaseClaimLostError
from tgvio.domain.archive import (
    ArchiveCapabilities,
    ArchivePackage,
    ArchivePackageState,
)
from tgvio.domain.job import Job
from tgvio.observability import log_event


_RECOVERABLE_ARCHIVE_STATES = (
    ArchivePackageState.PLANNED,
    ArchivePackageState.STAGING,
    ArchivePackageState.UPLOADING,
    ArchivePackageState.VERIFYING,
)


class ArchiveService:
    """Plan and execute one durable archive package per Job."""

    def __init__(
        self,
        repository: JobRepository,
        planner: ArchivePlanner,
        transport: ArchiveTransport,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._transport = transport
        self._executor = ArchiveExecutor(repository, transport)
        self._log = logging.getLogger("tgvio.archive")

    async def enqueue_job(self, job: Job) -> ArchivePackage | None:
        existing = await self._repository.get_archive_package_for_job(job.id)
        if existing is not None:
            log_event(
                self._log,
                logging.INFO,
                "archive.package.reused",
                job_id=job.id,
                package_id=existing.id,
                archive_state=existing.state.value,
            )
            return existing
        plan = self._planner.plan(job)
        package = await self._repository.save_archive_plan(plan)
        log_event(
            self._log,
            logging.INFO,
            "archive.package.planned",
            job_id=job.id,
            package_id=package.id,
            object_count=len(package.objects),
            total_bytes=sum(obj.size_bytes for obj in package.objects),
        )
        return package

    async def run_pending_once(self, *, limit: int = 10) -> int:
        packages = await self._repository.list_archive_packages_by_states(
            _RECOVERABLE_ARCHIVE_STATES,
            limit=limit,
        )
        processed = 0
        for package in packages:
            claim = PhaseClaimGuard(
                self._repository,
                package.job_id,
                "archive",
                ttl_seconds=600,
            )
            if not await claim.start():
                continue
            try:
                try:
                    await claim.run(self._executor.execute(package))
                except asyncio.CancelledError:
                    raise
                except PhaseClaimLostError:
                    log_event(
                        self._log,
                        logging.WARNING,
                        "archive.claim.lost",
                        "Archive execution stopped because its durable claim was lost",
                        package_id=package.id,
                        job_id=package.job_id,
                    )
                    continue
                except Exception as exc:
                    log_event(
                        self._log,
                        logging.ERROR,
                        "archive.package.failed",
                        "Archive package execution failed",
                        package_id=package.id,
                        job_id=package.job_id,
                        archive_state=package.state.value,
                        exception_type=type(exc).__name__,
                        exc_info=True,
                    )
                processed += 1
            finally:
                await claim.stop()
        return processed

    async def retry_package(self, package_id: str) -> ArchivePackage:
        package = await self._repository.get_archive_package(package_id)
        if package is None:
            raise KeyError(f"archive package not found: {package_id}")
        if package.state != ArchivePackageState.FAILED:
            raise ValueError(
                f"archive package must be failed before retry: {package.state.value}"
            )
        for obj in package.objects:
            local = Path(obj.local_path)
            if not local.is_file() or local.stat().st_size != obj.size_bytes:
                raise RuntimeError(
                    f"canonical cache unavailable for archive object {obj.object_index}"
                )
        reset = await self._repository.update_archive_package_state(
            package.id,
            ArchivePackageState.STAGING,
            event_type="archive_retry_requested",
            detail={"object_count": len(package.objects)},
            error_code=None,
            error_message=None,
        )
        log_event(
            self._log,
            logging.WARNING,
            "archive.package.retry_requested",
            package_id=package.id,
            job_id=package.job_id,
            object_count=len(package.objects),
        )
        return reset

    async def probe(self) -> ArchiveCapabilities:
        return await self._transport.probe()


class ArchiveRuntime:
    """Wakeable durable worker; failed packages require an explicit retry."""

    def __init__(
        self,
        service: ArchiveService,
        *,
        poll_seconds: float = 10.0,
    ) -> None:
        self._service = service
        self._poll_seconds = max(1.0, float(poll_seconds))
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="tgvio-archive-runtime")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def enqueue_job(self, job: Job) -> ArchivePackage | None:
        package = await self._service.enqueue_job(job)
        if package is not None and package.state in _RECOVERABLE_ARCHIVE_STATES:
            self._wake.set()
        return package

    async def retry_package(self, package_id: str) -> ArchivePackage:
        package = await self._service.retry_package(package_id)
        self._wake.set()
        return package

    async def probe(self) -> ArchiveCapabilities:
        return await self._service.probe()

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self._service.run_pending_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    logging.getLogger("tgvio.archive.runtime"),
                    logging.ERROR,
                    "archive.runtime.iteration_failed",
                    "Archive durable worker iteration failed",
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass
