from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import logging
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from tgvio.application.ports import JobRepository
from tgvio.domain.content import render_caption_template, split_template_buttons
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind, item_download_skipped
from tgvio.domain.publish import PublishPlan, PublishStep, PublishStepKind, PublishTarget
from tgvio.domain.progress import JobProgress
from tgvio.observability import log_event


@dataclass(frozen=True, slots=True)
class _RenderContext:
    """Frozen per-job values used to render the owner caption template."""

    template: str
    thumbnail_path: str
    channel_at: str
    group_at: str
    part_index: int
    part_count: int
    item_total: int
    rendered_at: datetime
    header_base: str = ""


_NO_GROUP = object()

_KIND_WORDS = {
    MediaKind.VIDEO: "个视频",
    MediaKind.PHOTO: "个图片",
    MediaKind.AUDIO: "个音频",
    MediaKind.DOCUMENT: "个文件",
}


def _with_header_number(header: str, display_no: int | None) -> str:
    """Insert the business-day task number after the leading date segment."""

    text = (header or "").strip()
    if not text or display_no is None:
        return text
    head, separator, rest = text.partition(" · ")
    if not separator:
        return f"{text} #{display_no}"
    return f"{head} #{display_no}{separator}{rest}"


@dataclass(frozen=True, slots=True)
class PlanningPolicy:
    cover_mode: bool = True
    cover_limit: int = 10
    album_limit: int = 10
    forward_caption: bool = True
    caption_footer: str = ""
    channel_at: str = ""
    group_at: str = ""


class JobOrchestrator:
    """Deterministically converts analyzed media facts into a durable publish plan."""

    def __init__(
        self,
        repository: JobRepository,
        policy: PlanningPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy or PlanningPolicy()
        self._active = self._policy
        self._clock = clock or self._default_clock
        self._render_context: _RenderContext | None = None
        self._log = logging.getLogger("tgvio.publish.plan")

    @staticmethod
    def _default_clock() -> datetime:
        return datetime.now(ZoneInfo("Asia/Shanghai"))

    def _resolve_policy(self, job: Job) -> PlanningPolicy:
        """Freeze per-job overrides (publish style snapshot) over the process baseline."""
        snapshot = job.policy.get("publish_style")
        if not isinstance(snapshot, dict):
            return self._policy
        override: dict[str, object] = {}
        if "cover_mode" in snapshot:
            override["cover_mode"] = bool(snapshot["cover_mode"])
        if "forward_caption" in snapshot:
            override["forward_caption"] = bool(snapshot["forward_caption"])
        if not override:
            return self._policy
        return replace(self._policy, **override)

    def plan(self, job: Job) -> PublishPlan:
        if job.state != JobState.ANALYZED:
            raise ValueError(f"job must be analyzed before planning: {job.state.value}")

        self._active = self._resolve_policy(job)
        steps: list[PublishStep] = []
        items = [
            item
            for item in sorted(job.items, key=lambda item: item.index)
            if not item_download_skipped(item)
        ]
        self._render_context = self._build_render_context(job, len(items))
        photos = [item for item in items if item.kind == MediaKind.PHOTO]
        videos = [item for item in items if item.kind == MediaKind.VIDEO]
        audios = [item for item in items if item.kind == MediaKind.AUDIO]
        documents = [
            item
            for item in items
            if item.kind in {MediaKind.DOCUMENT, MediaKind.AUDIO}
        ]
        collection_caption = str(job.policy.get("collection_caption", "") or "")
        chosen_cover = self._chosen_cover(job, items)
        merge = bool(job.policy.get("merge_album"))

        if merge and (photos or videos):
            # An owner-merged selection publishes as one channel cover post plus
            # merged albums (photos and videos together) in the discussion.
            cover_item = chosen_cover or (videos[0] if videos else photos[0])
            if cover_item.kind == MediaKind.VIDEO:
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.CHANNEL_VIDEO_COVER,
                        PublishTarget.CHANNEL,
                        [cover_item],
                        mode="generated_frame",
                        collection_caption=collection_caption,
                        collection_caption_item_index=cover_item.index,
                    )
                )
            else:
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.CHANNEL_COVER_ALBUM,
                        PublishTarget.CHANNEL,
                        [cover_item],
                        mode="photo_album",
                        collection_caption=collection_caption,
                        collection_caption_item_index=cover_item.index,
                    )
                )
            self._append_merged_album_steps(
                steps,
                [item for item in items if item.kind in {MediaKind.PHOTO, MediaKind.VIDEO}],
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
        elif self._active.cover_mode and (photos or videos):
            if chosen_cover is not None and chosen_cover.kind == MediaKind.VIDEO:
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.CHANNEL_VIDEO_COVER,
                        PublishTarget.CHANNEL,
                        [chosen_cover],
                        mode="generated_frame",
                        collection_caption=collection_caption,
                        collection_caption_item_index=chosen_cover.index,
                    )
                )
                for chunk, part_index, part_count in self._chunk_parts(
                    photos, self._active.album_limit
                ):
                    steps.append(
                        self._step(
                            steps,
                            PublishStepKind.DISCUSSION_PHOTO_ALBUM,
                            PublishTarget.DISCUSSION,
                            chunk,
                            mode="photo_album",
                            part=(part_index, part_count),
                        )
                    )
            else:
                ordered_photos = photos
                if chosen_cover is not None and chosen_cover.kind == MediaKind.PHOTO:
                    ordered_photos = [chosen_cover] + [
                        item for item in photos if item is not chosen_cover
                    ]
                cover_photos = ordered_photos[: self._active.cover_limit]
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

                overflow_photos = ordered_photos[self._active.cover_limit :]
                for chunk, part_index, part_count in self._chunk_parts(
                    overflow_photos, self._active.album_limit
                ):
                    steps.append(
                        self._step(
                            steps,
                            PublishStepKind.DISCUSSION_PHOTO_ALBUM,
                            PublishTarget.DISCUSSION,
                            chunk,
                            mode="photo_album",
                            part=(part_index, part_count),
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
            "audios": len(audios),
            "documents": len(documents),
            "steps": len(steps),
            "cover_mode": self._active.cover_mode,
            "cover_count": min(len(photos), self._active.cover_limit) if self._active.cover_mode else 0,
            "discussion_steps": sum(1 for step in steps if step.target == PublishTarget.DISCUSSION),
            "channel_steps": sum(1 for step in steps if step.target == PublishTarget.CHANNEL),
        }
        return PublishPlan(job_id=job.id, steps=tuple(steps), summary=summary)

    async def mark_planned(self, job: Job) -> PublishPlan:
        plan = self.plan(job)
        display_no = await self._display_no(job.id)
        if display_no is not None:
            steps = tuple(
                replace(
                    step,
                    params={
                        **step.params,
                        "caption_header": _with_header_number(
                            str(step.params.get("caption_header", "") or ""),
                            display_no,
                        ),
                    },
                )
                if step.params.get("caption_header")
                else step
                for step in plan.steps
            )
            plan = replace(plan, steps=steps)
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
            job_no=display_no,
            step_count=len(plan.steps),
            item_count=len(job.items),
            channel_steps=plan.summary.get("channel_steps", 0),
            discussion_steps=plan.summary.get("discussion_steps", 0),
        )
        return plan

    async def _display_no(self, job_id: str) -> int | None:
        getter = getattr(self._repository, "get_display_no", None)
        if not callable(getter):
            return None
        try:
            value = await getter(job_id)
        except Exception:  # noqa: BLE001 - a missing number must not block planning
            return None
        return None if value is None else int(value)

    @staticmethod
    def _chosen_cover(job: Job, items: list[MediaItem]) -> MediaItem | None:
        raw = job.policy.get("cover_item_index")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        if 0 <= raw < len(items):
            return items[raw]
        return None

    def _build_render_context(self, job: Job, item_total: int) -> _RenderContext:
        template = str(job.policy.get("caption_template", "") or "").strip()
        thumbnail_path = str(job.policy.get("thumbnail_path", "") or "").strip()
        part_index = self._policy_int(job, "collection_part_index", 0)
        part_count = self._policy_int(job, "collection_part_count", 0)
        rendered_at = self._clock()
        return _RenderContext(
            template=template,
            thumbnail_path=thumbnail_path,
            channel_at=self._active.channel_at,
            group_at=self._active.group_at,
            part_index=part_index,
            part_count=part_count,
            item_total=max(0, int(item_total)),
            rendered_at=rendered_at,
            header_base=self._header_base(job, rendered_at),
        )

    @classmethod
    def _header_base(cls, job: Job, rendered_at: datetime) -> str:
        """Owner-visible set identity shared by every step of one job.

        ``🗂 09-19 · 21 个媒体 · @source`` — the business-day task number is
        inserted later (``mark_planned``), once the repository assigned it.
        """

        counts: dict[MediaKind, int] = {}
        for item in job.items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        total = sum(counts.values())
        if total <= 0:
            return ""
        if len(counts) == 1:
            word = _KIND_WORDS.get(next(iter(counts)), "个媒体")
        else:
            word = "个媒体"
        segments = [f"🗂 {rendered_at.strftime('%m-%d')}", f"{total} {word}"]
        label = str(job.policy.get("source_label", "") or "").strip()
        if label:
            segments.append(label)
        return " · ".join(segments)

    def _section_header(self, part: tuple[int, int] | None) -> str:
        base = ""
        if self._render_context is not None:
            base = self._render_context.header_base
        if not base:
            return ""
        if part is not None and part[1] > 1:
            return f"{base} · 分卷 {part[0]}/{part[1]}"
        return base

    @staticmethod
    def _policy_int(job: Job, key: str, default: int) -> int:
        raw = job.policy.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int):
            return default
        return int(raw)

    def _step_caption_fields(
        self,
        batch: tuple[MediaItem, ...],
    ) -> dict[str, object]:
        """Render the owner caption template and parse optional inline buttons."""

        context = self._render_context
        if context is None or not context.template:
            return {}
        head = batch[0]
        variables = {
            "channel": context.channel_at,
            "group": context.group_at,
            "date": context.rendered_at.strftime("%Y-%m-%d"),
            "time": context.rendered_at.strftime("%H:%M"),
            "index": str(head.index + 1),
            "total": str(context.item_total),
            "count": str(len(batch)),
            "name": str(head.name or ""),
            "kind": head.kind.value,
            "part": str(context.part_index + 1) if context.part_count else "",
            "parts": str(context.part_count) if context.part_count else "",
        }
        try:
            rendered = render_caption_template(context.template, variables)
            rendered, buttons = split_template_buttons(rendered)
        except ValueError as exc:
            log_event(
                self._log,
                logging.WARNING,
                "publish.caption_template.invalid",
                "Owner caption template could not be rendered",
                exception_type=type(exc).__name__,
            )
            return {}
        fields: dict[str, object] = {}
        if rendered:
            fields["caption_template"] = rendered
        if buttons:
            fields["caption_buttons"] = tuple(buttons)
        return fields

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
        part: tuple[int, int] | None = None,
    ) -> PublishStep:
        batch = tuple(items)
        strategies = {str(item.index): self._strategy(item) for item in batch}
        header = self._section_header(part)
        params: dict[str, object] = {
            "mode": mode,
            "strategies": strategies,
            "forward_caption": self._active.forward_caption,
            "caption_footer": self._active.caption_footer,
            **(
                {
                    "collection_caption": collection_caption,
                    "collection_caption_item_index": collection_caption_item_index,
                }
                if collection_caption and collection_caption_item_index is not None
                else {}
            ),
            **(
                {
                    "caption_header": header,
                    "caption_header_item_index": batch[0].index,
                }
                if header and batch
                else {}
            ),
            **self._step_caption_fields(batch),
        }
        if self._render_context is not None and self._render_context.thumbnail_path:
            params["thumbnail_path"] = self._render_context.thumbnail_path
        return PublishStep(
            index=len(current_steps),
            kind=kind,
            target=target,
            item_indexes=tuple(item.index for item in batch),
            params=params,
        )

    def _append_video_steps(
        self,
        steps: list[PublishStep],
        videos: list[MediaItem],
        *,
        target: PublishTarget,
    ) -> None:
        album_buffer: list[MediaItem] = []
        buffer_group: object = _NO_GROUP

        def flush_album() -> None:
            nonlocal album_buffer, buffer_group
            if not album_buffer:
                return
            for chunk, part_index, part_count in self._chunk_parts(
                album_buffer, self._active.album_limit
            ):
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.DISCUSSION_VIDEO_ALBUM,
                        target,
                        chunk,
                        mode="video_album",
                        part=(part_index, part_count),
                    )
                )
            album_buffer = []
            buffer_group = _NO_GROUP

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
                continue
            if album_buffer and not self._same_album(buffer_group, item):
                flush_album()
            album_buffer.append(item)
            buffer_group = item.grouped_id
        flush_album()

    @staticmethod
    def _same_album(buffer_group: object, item: MediaItem) -> bool:
        """True when ``item`` continues the album currently buffered."""

        if buffer_group is _NO_GROUP:
            return False
        if item.grouped_id is None:
            return buffer_group is None
        return buffer_group == item.grouped_id

    def _append_merged_album_steps(
        self,
        steps: list[PublishStep],
        media_items: list[MediaItem],
    ) -> None:
        """Owner-merged selection: one album per ``album_limit`` items.

        ``grouped_id`` boundaries are deliberately ignored here (that is the whole
        point of merging), while oversized/split strategies still get their own
        step so a huge file is never forced into an album.
        """

        limit = self._active.album_limit
        buffer: list[MediaItem] = []

        def flush() -> None:
            nonlocal buffer
            if not buffer:
                return
            total = (len(buffer) + limit - 1) // limit
            for index in range(total):
                chunk = buffer[index * limit : (index + 1) * limit]
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.DISCUSSION_MEDIA_GROUP,
                        PublishTarget.DISCUSSION,
                        chunk,
                        mode="media_group",
                        part=(index + 1, total),
                    )
                )
            buffer = []

        for item in media_items:
            strategy = self._strategy(item)
            if strategy in {"split_playable", "document", "binary_volume"}:
                flush()
                document_mode = strategy in {"document", "binary_volume"}
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.DISCUSSION_DOCUMENT
                        if document_mode
                        else PublishStepKind.DISCUSSION_MEDIA,
                        PublishTarget.DISCUSSION,
                        [item],
                        mode="document" if document_mode else "video",
                    )
                )
                continue
            buffer.append(item)
        flush()

    def _append_channel_media_steps(
        self,
        steps: list[PublishStep],
        media_items: list[MediaItem],
    ) -> None:
        group_buffer: list[MediaItem] = []
        buffer_group: object = _NO_GROUP

        def flush_group() -> None:
            nonlocal group_buffer, buffer_group
            if not group_buffer:
                return
            for chunk, part_index, part_count in self._chunk_parts(
                group_buffer, self._active.album_limit
            ):
                steps.append(
                    self._step(
                        steps,
                        PublishStepKind.CHANNEL_MEDIA_GROUP,
                        PublishTarget.CHANNEL,
                        chunk,
                        mode="media_group" if len(chunk) > 1 else chunk[0].kind.value,
                        part=(part_index, part_count),
                    )
                )
            group_buffer = []
            buffer_group = _NO_GROUP

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
                continue
            if group_buffer and not self._same_album(buffer_group, item):
                flush_group()
            group_buffer.append(item)
            buffer_group = item.grouped_id
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

    @classmethod
    def _chunks(cls, items: list[MediaItem], size: int) -> Iterable[list[MediaItem]]:
        for chunk, _part_index, _part_count in cls._chunk_parts(items, size):
            yield chunk

    @classmethod
    def _chunk_parts(
        cls,
        items: list[MediaItem],
        size: int,
    ) -> Iterable[tuple[list[MediaItem], int, int]]:
        """Chunk items while never splitting one source album across chunks.

        ``grouped_id`` marks a native source album; its members always stay in the
        same step (unless the album itself exceeds ``size``, in which case every
        chunk is labelled ``分卷 i/n``). Items without a ``grouped_id`` keep the
        previous "pack consecutive loose media together" behaviour.
        """

        if not items:
            return
        chunks: list[list[MediaItem]] = []
        for run in cls._album_runs(items):
            for start in range(0, len(run), size):
                chunks.append(run[start : start + size])
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            yield (chunk, index, total)

    @staticmethod
    def _album_runs(items: list[MediaItem]) -> Iterable[list[MediaItem]]:
        current: list[MediaItem] = []
        group: object = _NO_GROUP
        for item in items:
            value = item.grouped_id
            if current and (
                (value is not None and value == group)
                or (value is None and group is None)
            ):
                current.append(item)
                continue
            if current:
                yield current
            current = [item]
            group = value if value is not None else None
        if current:
            yield current
