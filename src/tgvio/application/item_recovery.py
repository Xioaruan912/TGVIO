from __future__ import annotations

from tgvio.application.ports import JobRepository
from tgvio.domain.job import Job


class SkippedItemRecoveryUnavailableError(RuntimeError):
    """The requested Job cannot safely produce a skipped-item recovery child."""


class SkippedItemRecoveryService:
    """Create the one durable received child containing a terminal Job's skipped media."""

    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    async def recover(self, *, parent_job_id: str, owner_id: int) -> Job:
        try:
            return await self._repository.create_skipped_item_recovery(
                parent_job_id,
                owner_id=int(owner_id),
            )
        except (KeyError, PermissionError, ValueError) as exc:
            raise SkippedItemRecoveryUnavailableError(
                "skipped-item recovery is unavailable"
            ) from exc
