from __future__ import annotations

import logging

from tgvio.application.job_control import JobCancelRequested, JobControlService
from tgvio.application.ports import JobRepository, MediaInspector
from tgvio.domain.job import Job, JobState
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event


class MediaAnalyzer:
    def __init__(
        self,
        repository: JobRepository,
        inspector: MediaInspector,
        control: JobControlService | None = None,
    ) -> None:
        self._repository = repository
        self._inspector = inspector
        self._control = control
        self._log = logging.getLogger("tgvio.analysis")

    async def analyze(self, job: Job) -> Job:
        await self._cancel_checkpoint(job, "cancelled before analysis started")
        if job.state == JobState.DOWNLOADED:
            job = await self._repository.transition(
                job.id,
                JobState.ANALYZING,
                event_type="analysis_started",
            )
        elif job.state != JobState.ANALYZING:
            raise ValueError(f"job must be downloaded/analyzing before analysis: {job.state.value}")
        log_event(
            self._log,
            logging.INFO,
            "analysis.job.started",
            job_id=job.id,
            item_count=len(job.items),
        )
        analyzed = []
        try:
            for position, item in enumerate(job.items):
                await self._repository.set_job_progress(
                    JobProgress(
                        job_id=job.id,
                        phase="analyzing",
                        current=position,
                        total=len(job.items),
                        item_index=item.index,
                        item_total=len(job.items),
                    )
                )
                await self._cancel_checkpoint(
                    job,
                    f"cancelled before analysis item {item.index}",
                )
                analyzed.append(await self._inspector.inspect(item))
                await self._cancel_checkpoint(
                    job,
                    f"cancelled after analysis item {item.index}",
                )
        except JobCancelRequested:
            raise
        except Exception as exc:
            log_event(
                self._log,
                logging.ERROR,
                "analysis.job.failed",
                "Media analysis failed",
                job_id=job.id,
                error_code="media_analysis_failed",
                exception_type=type(exc).__name__,
                exc_info=True,
            )
            await self._repository.set_job_progress(
                JobProgress(
                    job_id=job.id,
                    phase="failed",
                    detail_code="media_analysis_failed",
                    item_total=len(job.items),
                )
            )
            await self._repository.transition(
                job.id,
                JobState.FAILED,
                event_type="analysis_failed",
                error_code="media_analysis_failed",
                error_message=str(exc),
            )
            raise
        job.items = analyzed
        await self._repository.save(job)
        completed = await self._repository.transition(
            job.id,
            JobState.ANALYZED,
            event_type="analysis_completed",
        )
        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="analyzed",
                current=len(job.items),
                total=len(job.items),
                item_total=len(job.items),
            )
        )
        log_event(
            self._log,
            logging.INFO,
            "analysis.job.completed",
            job_id=job.id,
            item_count=len(job.items),
        )
        return completed

    async def _cancel_checkpoint(self, job: Job, detail: str) -> None:
        if self._control is not None:
            await self._control.checkpoint(job, detail=detail)
