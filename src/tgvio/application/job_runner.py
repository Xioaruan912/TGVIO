from __future__ import annotations

import logging

from tgvio.application.execution import PublishExecutionEngine
from tgvio.application.ports import ArchiveEnqueuer, JobRepository
from tgvio.application.processor import IngestionProcessor
from tgvio.domain.job import Job, JobState
from tgvio.observability import log_event


class JobRunner:
    """Runs one durable job through planning and, when enabled, publishing."""

    def __init__(
        self,
        repository: JobRepository,
        ingestion: IngestionProcessor,
        execution: PublishExecutionEngine | None = None,
        archive: ArchiveEnqueuer | None = None,
    ) -> None:
        self._repository = repository
        self._ingestion = ingestion
        self._execution = execution
        self._archive = archive
        self._log = logging.getLogger("tgvio.job.runner")

    @property
    def repository(self) -> JobRepository:
        return self._repository

    @property
    def publish_enabled(self) -> bool:
        return self._execution is not None

    async def prepare(self, job: Job) -> Job:
        """Advance one Job through download/analyze/plan, but never publish it."""
        log_event(
            self._log,
            logging.INFO,
            "job.prepare.started",
            job_id=job.id,
            state=job.state.value,
            item_count=len(job.items),
        )
        if job.state in {
            JobState.RECEIVED,
            JobState.DOWNLOADING,
            JobState.DOWNLOADED,
            JobState.ANALYZING,
            JobState.ANALYZED,
        }:
            job = await self._ingestion.process(job)
        if job.state == JobState.PLANNED and self._archive is not None:
            package = await self._archive.enqueue_job(job)
            if package is not None:
                log_event(
                    self._log,
                    logging.INFO,
                    "job.archive.enqueued",
                    job_id=job.id,
                    package_id=package.id,
                    archive_state=package.state.value,
                    object_count=len(package.objects),
                )
        return job

    async def publish(self, job: Job) -> Job:
        """Publish one already planned/recovering Job without running ingestion."""
        if self._execution is None:
            return job
        if job.state not in {JobState.PLANNED, JobState.PUBLISHING}:
            return job
        plan = await self._repository.get_publish_plan(job.id)
        if plan is None:
            raise RuntimeError(f"{job.state.value} job has no PublishPlan: {job.id}")
        result = await self._execution.execute(job, plan)
        log_event(
            self._log,
            logging.INFO,
            "job.run.completed",
            job_id=job.id,
            state=result.state.value,
        )
        return result

    async def process(self, job: Job) -> Job:
        """Compatibility path used by tests/tools outside the durable dispatcher."""
        log_event(
            self._log,
            logging.INFO,
            "job.run.started",
            job_id=job.id,
            state=job.state.value,
            item_count=len(job.items),
        )
        job = await self.prepare(job)
        if not self.publish_enabled and job.state == JobState.PLANNED:
            log_event(
                self._log,
                logging.INFO,
                "job.run.paused",
                job_id=job.id,
                state=job.state.value,
                reason="publish_disabled",
            )
            return job
        return await self.publish(job)
