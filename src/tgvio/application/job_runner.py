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

    async def process(self, job: Job) -> Job:
        log_event(
            self._log,
            logging.INFO,
            "job.run.started",
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
        if job.state != JobState.PLANNED:
            if job.state == JobState.PUBLISHING and self._execution is not None:
                plan = await self._repository.get_publish_plan(job.id)
                if plan is None:
                    raise RuntimeError(f"publishing job has no PublishPlan: {job.id}")
                result = await self._execution.execute(job, plan)
                log_event(
                    self._log,
                    logging.INFO,
                    "job.run.completed",
                    job_id=job.id,
                    state=result.state.value,
                )
                return result
            return job
        if self._archive is not None:
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
        if self._execution is None:
            log_event(
                self._log,
                logging.INFO,
                "job.run.paused",
                job_id=job.id,
                state=job.state.value,
                reason="publish_disabled",
            )
            return job
        plan = await self._repository.get_publish_plan(job.id)
        if plan is None:
            raise RuntimeError(f"planned job has no PublishPlan: {job.id}")
        result = await self._execution.execute(job, plan)
        log_event(
            self._log,
            logging.INFO,
            "job.run.completed",
            job_id=job.id,
            state=result.state.value,
        )
        return result
