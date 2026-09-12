from __future__ import annotations

from tgvio.application.job_control import JobControlService
from tgvio.application.media_analyzer import MediaAnalyzer
from tgvio.application.media_downloader import JobDownloader
from tgvio.application.orchestrator import JobOrchestrator
from tgvio.application.reference_cache import TelegramReferenceEnricher
from tgvio.domain.job import Job, JobState


class IngestionProcessor:
    """Runs the durable pre-publish pipeline for one accepted job."""

    def __init__(
        self,
        downloader: JobDownloader,
        analyzer: MediaAnalyzer,
        orchestrator: JobOrchestrator,
        reference_enricher: TelegramReferenceEnricher | None = None,
        control: JobControlService | None = None,
    ) -> None:
        self._downloader = downloader
        self._analyzer = analyzer
        self._orchestrator = orchestrator
        self._reference_enricher = reference_enricher
        self._control = control

    async def process(self, job: Job) -> Job:
        if job.state in {JobState.RECEIVED, JobState.DOWNLOADING}:
            job = await self._downloader.download(job)
        if job.state in {JobState.DOWNLOADED, JobState.ANALYZING}:
            job = await self._analyzer.analyze(job)
        if job.state == JobState.ANALYZED:
            if self._control is not None:
                await self._control.safe_checkpoint(
                    job,
                    detail="paused before publish planning",
                )
            if self._reference_enricher is not None:
                job = await self._reference_enricher.enrich(job)
            await self._orchestrator.mark_planned(job)
            return job
        if job.state == JobState.PLANNED:
            return job
        raise ValueError(f"job is not recoverable by ingestion processor: {job.state.value}")
