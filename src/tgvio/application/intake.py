from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from tgvio.application.ports import JobRepository
from tgvio.domain.job import Job, MediaItem, MediaKind


@dataclass(frozen=True, slots=True)
class IncomingMedia:
    kind: MediaKind
    source: str
    caption: str = ""
    size_bytes: int = 0
    name: str | None = None
    spoiler: bool = False
    grouped_id: int | None = None
    source_chat_id: int | None = None
    source_message_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class IntakeService:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    async def accept(
        self,
        *,
        owner_id: int,
        destination: str,
        media: Sequence[IncomingMedia],
    ) -> Job:
        if not media:
            raise ValueError("incoming media batch must not be empty")
        items = [
            MediaItem(
                index=index,
                kind=item.kind,
                source=item.source,
                caption=item.caption,
                size_bytes=item.size_bytes,
                name=item.name,
                spoiler=item.spoiler,
                grouped_id=item.grouped_id,
                source_chat_id=item.source_chat_id,
                source_message_id=item.source_message_id,
                metadata=dict(item.metadata),
            )
            for index, item in enumerate(media)
        ]
        job = Job(owner_id=owner_id, destination=destination, items=items)
        await self._repository.create(job)
        return job
