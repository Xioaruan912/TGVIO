from __future__ import annotations

from dataclasses import replace

from tgvio.application.ports import JobRepository
from tgvio.domain.job import Job, JobState


class TelegramReferenceEnricher:
    """Attach durable opaque Telegram references to analyzed media items."""

    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    async def enrich(self, job: Job) -> Job:
        if job.state != JobState.ANALYZED:
            raise ValueError(f"job must be analyzed before reference lookup: {job.state.value}")
        changed = False
        items = []
        for item in job.items:
            if item.telegram_ref or not item.sha256:
                items.append(item)
                continue
            reference = await self._repository.get_telegram_reference(
                item.sha256,
                job.destination,
                item.kind,
            )
            if not reference:
                items.append(item)
                continue
            metadata = dict(item.metadata)
            metadata["telegram_reference_cache_hit"] = True
            items.append(replace(item, telegram_ref=reference, metadata=metadata))
            changed = True
        if changed:
            job.items = items
            await self._repository.save(job)
        return job
