from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from typing import Iterable

from tgvio.application.ports import JobRepository
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.publish import PublishPlan, PublishStep, PublishStepKind, PublishTarget
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event


@dataclass(frozen=True, slots=True)
class PlanningPolicy:
    cover_mode: bool = True
    cover_limit: int = 10
    album_limit: int = 10
    forward_caption: bool = True
    caption_footer: str = ""


class JobOrchestrator:
    """Deterministically converts analyzed media facts into a durable publish plan."""

    def __init__(self, repository: JobRepository, policy: PlanningPolicy | None = None) -> None:
        self._repository = repository
        self._policy = policy or PlanningPolicy()
        self._log = logging.getLogger("tgvio.publish.plan")

    def plan(self, job: Job) -> PublishPlan:
        if job.state != JobState.ANALYZED:
            raise ValueError(f"job must be analyzed before planning: {job.state.value}")

        steps: list[PublishStep] = []
        items = sorted(job.items, key=lambda item: item.index)
        photos = [item for item in items if item.kind == MediaKind.PHOTO]
        videos = [item for item in items if item.kind == MediaKind.VIDEO]
        documents = [item for item in items if item.kind == MediaKind.DOCUMENT]
        collection_caption = str(job.policy.get("collection_caption", "") or "")

        if self._policy.cover_mode and (photos or videos):
            cover_photos = photos[: self._policy.cover_limit]
            if cover_photos:
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.CHANNEL_COVER_ALBUM,
                        PublishTarget.CHANNEL,
                        cover_photos,
                        mode="photo_album",
                        collection_caption=collection_caption,
                        collection_caption_item_index=cover_photos[0].index,
                    )
                )
            elif videos:
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.CHANNEL_VIDEO_COVER,
                        PublishTarget.CHANNEL,
                        [videos[0]],
                        mode="generated_frame",
                        collection_caption=collection_caption,
                        collection_caption_item_index=videos[0].index,
                    )
                )

            overflow_photos = photos[self._policy.cover_limit :]
            for chunk in self._chunks(overflow_photos, self._policy.album_limit):
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.DISCUSSION_PHOTO_ALBUM,
                        PublishTarget.DISCUSSION,
                        chunk,
                        mode="photo_album",
                    )
                )

            self._append_video_steps(
                steps,
                videos,
                target=PublishTarget.DISCUSSION,
            )

            for item in documents:
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.DISCUSSION_DOCUMENT,
                        PublishTarget.DISCUSSION,
                        [item],
                        mode="document",
                    )
                )
        else:
            media_items = [item for item in items if item.kind in {MediaKind.PHOTO, MediaKind.VIDEO}]
            self._append_channel_media_steps(steps, media_items)
            for item in documents:
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.CHANNEL_DOCUMENT,
                        PublishTarget.CHANNEL,
                        [item],
                        mode="document",
                    )
                )

        if collection_caption and steps and not any(
            step.params.get("collection_caption") for step in steps
        ):
            first_visible = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if step.target == PublishTarget.CHANNEL and step.item_indexes
                ),
                None,
            )
            if first_visible is not None:
                step = steps[first_visible]
                steps[first_visible] = replace(
                    step,
                    params={
                        **step.params,
                        "collection_caption": collection_caption,
                        "collection_caption_item_index": step.item_indexes[0],
                    },
                )

        summary = {
            "media_total": len(items),
            "photos": len(photos),
            "videos": len(videos),
            "documents": len(documents),
            "steps": len(steps),
            "cover_mode": self._policy.cover_mode,
            "cover_count": min(len(photos), self._policy.cover_limit) if self._policy.cover_mode else 0,
            "discussion_steps": sum(1 for step in steps if step.target == PublishTarget.DISCUSSION),
            "channel_steps": sum(1 for step in steps if step.target == PublishTarget.CHANNEL),
        }
        return PublishPlan(job_id=job.id, steps=tuple(steps), summary=summary)

    async def mark_planned(self, job: Job) -> PublishPlan:
        plan = self.plan(job)
        await self._repository.save_publish_plan(plan)
        await self._repository.transition(
            job.id,
            JobState.PLANNED,
            event_type="publish_plan_created",
            detail=f"plan={plan.id};steps={len(plan.steps)};version={plan.version}",
        )
        await self._repository.set_job_progress(
            JobProgress(
                job_id=job.id,
                phase="planned",
                current=len(plan.steps),
                total=len(plan.steps),
                item_total=len(job.items),
            )
        )
        job.state = JobState.PLANNED
        log_event(
            self._log,
            logging.INFO,
            "publish.plan.created",
            job_id=job.id,
            plan_id=plan.id,
            step_count=len(plan.steps),
            item_count=len(job.items),
            channel_steps=plan.summary.get("channel_steps", 0),
            discussion_steps=plan.summary.get("discussion_steps", 0),
        )
        return plan

    def _step(
        self,
        current_steps: list[PublishStep],
        kind: PublishStepKind,
        target: PublishTarget,
        items: Iterable[MediaItem],
        *,
        mode: str,
        collection_caption: str = "",
        collection_caption_item_index: int | None = None,
    ) -> PublishStep:
        batch = tuple(items)
        strategies = {str(item.index): self._strategy(item) for item in batch}
        return PublishStep(
            index=len(current_steps),
            kind=kind,
            target=target,
            item_indexes=tuple(item.index for item in batch),
            params={
                "mode": mode,
                "strategies": strategies,
                "forward_caption": self._policy.forward_caption,
                "caption_footer": self._policy.caption_footer,
                **(
                    {
                        "collection_caption": collection_caption,
                        "collection_caption_item_index": collection_caption_item_index,
                    }
                    if collection_caption and collection_caption_item_index is not None
                    else {}
                ),
            },
        )

    def _append_video_steps(
        self,
        steps: list[PublishStep],
        videos: list[MediaItem],
        *,
        target: PublishTarget,
    ) -> None:
        album_buffer: list[MediaItem] = []

        def flush_album() -> None:
            nonlocal album_buffer
            if not album_buffer:
                return
            for chunk in self._chunks(album_buffer, self._policy.album_limit):
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.DISCUSSION_VIDEO_ALBUM,
                        target,
                        chunk,
                        mode="video_album",
                    )
                )
            album_buffer = []

        for item in videos:
            strategy = self._strategy(item)
            if strategy in {"split_playable", "document", "binary_volume"}:
                flush_album()
                document_mode = strategy in {"document", "binary_volume"}
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.DISCUSSION_DOCUMENT if document_mode else PublishStepKind.DISCUSSION_MEDIA,
                        target,
                        [item],
                        mode="document" if document_mode else "video",
                    )
                )
            else:
                album_buffer.append(item)
                if len(album_buffer) >= self._policy.album_limit:
                    flush_album()
        flush_album()

    def _append_channel_media_steps(
        self,
        steps: list[PublishStep],
        media_items: list[MediaItem],
    ) -> None:
        group_buffer: list[MediaItem] = []

        def flush_group() -> None:
            nonlocal group_buffer
            if not group_buffer:
                return
            for chunk in self._chunks(group_buffer, self._policy.album_limit):
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.CHANNEL_MEDIA_GROUP,
                        PublishTarget.CHANNEL,
                        chunk,
                        mode="media_group" if len(chunk) > 1 else chunk[0].kind.value,
                    )
                )
            group_buffer = []

        for item in media_items:
            strategy = self._strategy(item)
            if strategy in {"split_playable", "document", "binary_volume"}:
                flush_group()
                document_mode = strategy in {"document", "binary_volume"}
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.CHANNEL_DOCUMENT if document_mode else PublishStepKind.CHANNEL_MEDIA_GROUP,
                        PublishTarget.CHANNEL,
                        [item],
                        mode="document" if document_mode else item.kind.value,
                    )
                )
            else:
                group_buffer.append(item)
                if len(group_buffer) >= self._policy.album_limit:
                    flush_group()
        flush_group()

    @staticmethod
    def _strategy(item: MediaItem) -> str:
        if item.telegram_ref:
            return "reuse_reference"
        metadata = item.metadata
        if metadata.get("large_file"):
            if item.kind == MediaKind.VIDEO and metadata.get("telegram_streamable_candidate"):
                return "split_playable"
            return "binary_volume"
        if metadata.get("send_as_document_candidate"):
            return "document"
        if metadata.get("faststart_candidate"):
            return "remux_faststart"
        return "native"

    @staticmethod
    def _chunks(items: list[MediaItem], size: int) -> Iterable[list[MediaItem]]:
        for start in range(0, len(items), size):
            yield items[start : start + size]
