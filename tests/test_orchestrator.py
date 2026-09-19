from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from tgvio.application.orchestrator import JobOrchestrator, PlanningPolicy
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.publish import PublishStepKind, PublishTarget


class DummyRepository:
    pass


class _DisplayNumberRepository:
    def __init__(self, display_no: int | None) -> None:
        self.display_no = display_no
        self.saved = None

    async def get_display_no(self, job_id: str) -> int | None:
        return self.display_no

    async def save_publish_plan(self, plan) -> None:
        self.saved = plan

    async def transition(self, *args, **kwargs):
        return None

    async def set_job_progress(self, progress) -> None:
        return None


class MarkPlannedNumberingTests(unittest.IsolatedAsyncioTestCase):
    def _job(self) -> Job:
        return Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=[
                MediaItem(index=i, kind=MediaKind.VIDEO, source=f"v{i}") for i in range(2)
            ],
        )

    def _orchestrator(self, repo) -> JobOrchestrator:
        return JobOrchestrator(
            repo,
            PlanningPolicy(),
            clock=lambda: datetime(2026, 9, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    async def test_business_day_number_is_inserted_into_the_header(self) -> None:
        repo = _DisplayNumberRepository(15)
        plan = await self._orchestrator(repo).mark_planned(self._job())
        self.assertEqual(
            plan.steps[0].params["caption_header"], "🗂 09-15 #15 · 2 个视频"
        )
        self.assertIs(repo.saved, plan)

    async def test_missing_number_leaves_the_header_numberless(self) -> None:
        repo = _DisplayNumberRepository(None)
        plan = await self._orchestrator(repo).mark_planned(self._job())
        self.assertEqual(plan.steps[0].params["caption_header"], "🗂 09-15 · 2 个视频")

    async def test_repository_without_display_numbers_is_tolerated(self) -> None:
        plan = await self._orchestrator(_DisplayNumberRepository(None)).mark_planned(
            self._job()
        )
        self.assertIn("09-15", plan.steps[0].params["caption_header"])


class OrchestratorTests(unittest.TestCase):
    def test_ten_is_cover_display_limit_not_media_limit(self) -> None:
        photos = [
            MediaItem(index=i, kind=MediaKind.PHOTO, source=f"photo-{i}")
            for i in range(12)
        ]
        video = MediaItem(
            index=12,
            kind=MediaKind.VIDEO,
            source="video-1",
            metadata={"telegram_streamable_candidate": True},
        )
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=photos + [video],
        )
        plan = JobOrchestrator(DummyRepository()).plan(job)
        self.assertEqual(plan.steps[0].kind, PublishStepKind.CHANNEL_COVER_ALBUM)
        self.assertEqual(plan.steps[0].item_indexes, tuple(range(10)))
        self.assertEqual(plan.steps[1].kind, PublishStepKind.DISCUSSION_PHOTO_ALBUM)
        self.assertEqual(plan.steps[1].item_indexes, (10, 11))
        self.assertEqual(plan.steps[2].kind, PublishStepKind.DISCUSSION_VIDEO_ALBUM)
        self.assertEqual(plan.steps[2].item_indexes, (12,))
        self.assertEqual(plan.summary["media_total"], 13)

    def test_video_only_cover_mode_generates_channel_cover_step(self) -> None:
        videos = [
            MediaItem(
                index=i,
                kind=MediaKind.VIDEO,
                source=f"video-{i}",
                metadata={"telegram_streamable_candidate": True},
            )
            for i in range(3)
        ]
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=videos,
        )
        plan = JobOrchestrator(DummyRepository()).plan(job)
        self.assertEqual(plan.steps[0].kind, PublishStepKind.CHANNEL_VIDEO_COVER)
        self.assertEqual(plan.steps[0].target, PublishTarget.CHANNEL)
        self.assertEqual(plan.steps[1].kind, PublishStepKind.DISCUSSION_VIDEO_ALBUM)
        self.assertEqual(plan.steps[1].item_indexes, (0, 1, 2))

    def test_large_and_incompatible_videos_get_explicit_strategies(self) -> None:
        native = MediaItem(
            index=0,
            kind=MediaKind.VIDEO,
            source="native",
            metadata={"telegram_streamable_candidate": True},
        )
        split = replace(
            native,
            index=1,
            source="large",
            metadata={"telegram_streamable_candidate": True, "large_file": True},
        )
        document = replace(
            native,
            index=2,
            source="avi",
            metadata={"send_as_document_candidate": True},
        )
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=[native, split, document],
        )
        plan = JobOrchestrator(DummyRepository()).plan(job)
        strategy_by_item = {}
        for step in plan.steps:
            strategy_by_item.update({int(key): value for key, value in step.params["strategies"].items()})
        self.assertEqual(strategy_by_item[0], "native")
        self.assertEqual(strategy_by_item[1], "split_playable")
        self.assertEqual(strategy_by_item[2], "document")
        split_step = next(step for step in plan.steps if step.item_indexes == (1,))
        document_step = next(step for step in plan.steps if step.item_indexes == (2,))
        self.assertEqual(split_step.kind, PublishStepKind.DISCUSSION_MEDIA)
        self.assertEqual(document_step.kind, PublishStepKind.DISCUSSION_DOCUMENT)

    def test_direct_mode_targets_channel_only(self) -> None:
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=[
                MediaItem(index=0, kind=MediaKind.PHOTO, source="p"),
                MediaItem(
                    index=1,
                    kind=MediaKind.VIDEO,
                    source="v",
                    metadata={"telegram_streamable_candidate": True},
                ),
            ],
        )
        plan = JobOrchestrator(
            DummyRepository(),
            PlanningPolicy(cover_mode=False),
        ).plan(job)
        self.assertTrue(plan.steps)
        self.assertTrue(all(step.target == PublishTarget.CHANNEL for step in plan.steps))

    def test_direct_mode_isolates_large_video_from_album(self) -> None:
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=[
                MediaItem(index=0, kind=MediaKind.PHOTO, source="photo"),
                MediaItem(
                    index=1,
                    kind=MediaKind.VIDEO,
                    source="large",
                    metadata={"large_file": True, "telegram_streamable_candidate": True},
                ),
                MediaItem(index=2, kind=MediaKind.PHOTO, source="photo-2"),
            ],
        )
        plan = JobOrchestrator(
            DummyRepository(),
            PlanningPolicy(cover_mode=False),
        ).plan(job)
        large_step = next(step for step in plan.steps if step.item_indexes == (1,))
        self.assertEqual(large_step.params["strategies"]["1"], "split_playable")
        self.assertEqual(large_step.target, PublishTarget.CHANNEL)

    def test_collection_text_is_frozen_only_into_first_channel_cover_caption(self) -> None:
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            policy={"collection_caption": "第一行\n第二行"},
            items=[
                MediaItem(index=0, kind=MediaKind.PHOTO, source="photo-0"),
                MediaItem(index=1, kind=MediaKind.PHOTO, source="photo-1"),
                MediaItem(
                    index=2,
                    kind=MediaKind.VIDEO,
                    source="video",
                    metadata={"telegram_streamable_candidate": True},
                ),
            ],
        )
        plan = JobOrchestrator(DummyRepository()).plan(job)
        cover = plan.steps[0]
        self.assertEqual(cover.kind, PublishStepKind.CHANNEL_COVER_ALBUM)
        self.assertEqual(cover.params["collection_caption"], "第一行\n第二行")
        self.assertEqual(cover.params["collection_caption_item_index"], 0)
        self.assertTrue(
            all(
                "collection_caption" not in step.params
                for step in plan.steps[1:]
            )
        )

    def test_collection_text_is_not_lost_in_direct_or_document_only_mode(self) -> None:
        for item in (
            MediaItem(index=0, kind=MediaKind.PHOTO, source="photo"),
            MediaItem(index=0, kind=MediaKind.DOCUMENT, source="document"),
        ):
            with self.subTest(kind=item.kind.value):
                job = Job(
                    owner_id=42,
                    destination="@destination",
                    state=JobState.ANALYZED,
                    policy={"collection_caption": "合集文字"},
                    items=[item],
                )
                plan = JobOrchestrator(
                    DummyRepository(),
                    PlanningPolicy(cover_mode=False),
                ).plan(job)
                self.assertEqual(plan.steps[0].target, PublishTarget.CHANNEL)
                self.assertEqual(plan.steps[0].params["collection_caption"], "合集文字")
                self.assertEqual(plan.steps[0].params["collection_caption_item_index"], 0)

    def test_caption_policy_is_persisted_into_every_step(self) -> None:
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="photo")],
        )
        plan = JobOrchestrator(
            DummyRepository(),
            PlanningPolicy(
                cover_mode=False,
                forward_caption=False,
                caption_footer="@destination @discussion",
            ),
        ).plan(job)
        self.assertEqual(len(plan.steps), 1)
        self.assertFalse(plan.steps[0].params["forward_caption"])
        self.assertEqual(
            plan.steps[0].params["caption_footer"],
            "@destination @discussion",
        )

    def test_caption_template_renders_variables_into_step_params(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            policy={
                "caption_template": "{channel} | {date} 第{index}集/共{total}集 {count}{kind}",
                "collection_part_index": 1,
                "collection_part_count": 3,
            },
            items=[
                MediaItem(index=0, kind=MediaKind.VIDEO, source="v0", name="ep1.mp4"),
                MediaItem(index=1, kind=MediaKind.VIDEO, source="v1", name="ep2.mp4"),
            ],
        )
        orchestrator = JobOrchestrator(
            DummyRepository(),
            PlanningPolicy(channel_at="@channel", group_at="@group"),
            clock=lambda: datetime(2026, 9, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        plan = orchestrator.plan(job)
        album_steps = [
            step for step in plan.steps if len(step.item_indexes) == 2
        ]
        self.assertEqual(len(album_steps), 1)
        self.assertEqual(
            album_steps[0].params["caption_template"],
            "@channel | 2026-09-15 第1集/共2集 2video",
        )

    def test_set_header_marks_the_first_item_of_every_step(self) -> None:
        photos = [
            MediaItem(index=i, kind=MediaKind.PHOTO, source=f"photo-{i}")
            for i in range(12)
        ]
        video = MediaItem(
            index=12,
            kind=MediaKind.VIDEO,
            source="video-0",
            metadata={"telegram_streamable_candidate": True},
        )
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=photos + [video],
        )
        orchestrator = JobOrchestrator(
            DummyRepository(),
            PlanningPolicy(),
            clock=lambda: datetime(2026, 9, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        plan = orchestrator.plan(job)
        for step in plan.steps:
            self.assertEqual(step.params["caption_header"], "🗂 09-15 · 13 个媒体")
            self.assertEqual(
                step.params["caption_header_item_index"], step.item_indexes[0]
            )

    def test_source_label_is_part_of_the_header(self) -> None:
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            policy={"source_label": "@xiaodeFile_bot"},
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="v0")],
        )
        orchestrator = JobOrchestrator(
            DummyRepository(),
            PlanningPolicy(),
            clock=lambda: datetime(2026, 9, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        plan = orchestrator.plan(job)
        self.assertEqual(
            plan.steps[0].params["caption_header"],
            "🗂 09-15 · 1 个视频 · @xiaodeFile_bot",
        )

    def test_source_albums_are_never_mixed_into_one_step(self) -> None:
        policy = PlanningPolicy(cover_mode=False)
        first_album = [
            MediaItem(index=i, kind=MediaKind.VIDEO, source=f"a{i}", grouped_id=11)
            for i in range(5)
        ]
        second_album = [
            MediaItem(index=5 + i, kind=MediaKind.VIDEO, source=f"b{i}", grouped_id=22)
            for i in range(7)
        ]
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=first_album + second_album,
        )
        plan = JobOrchestrator(DummyRepository(), policy).plan(job)
        self.assertEqual(
            [step.item_indexes for step in plan.steps],
            [(0, 1, 2, 3, 4), (5, 6, 7, 8, 9, 10, 11)],
        )
        self.assertTrue(
            all("分卷" not in step.params["caption_header"] for step in plan.steps)
        )

    def test_oversized_album_is_labelled_with_parts(self) -> None:
        policy = PlanningPolicy(cover_mode=False)
        album = [
            MediaItem(index=i, kind=MediaKind.VIDEO, source=f"v{i}", grouped_id=99)
            for i in range(12)
        ]
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=album,
        )
        plan = JobOrchestrator(DummyRepository(), policy).plan(job)
        self.assertEqual(
            [step.item_indexes for step in plan.steps], [tuple(range(10)), (10, 11)]
        )
        self.assertIn("分卷 1/2", plan.steps[0].params["caption_header"])
        self.assertIn("分卷 2/2", plan.steps[1].params["caption_header"])

    def test_loose_media_still_packs_into_album_sized_steps(self) -> None:
        policy = PlanningPolicy(cover_mode=False)
        items = [
            MediaItem(index=i, kind=MediaKind.VIDEO, source=f"v{i}") for i in range(12)
        ]
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            items=items,
        )
        plan = JobOrchestrator(DummyRepository(), policy).plan(job)
        self.assertEqual(
            [step.item_indexes for step in plan.steps], [tuple(range(10)), (10, 11)]
        )

    def test_caption_template_button_line_becomes_caption_buttons(self) -> None:
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            policy={"caption_template": "看这里\nbutton: 打开频道 | https://t.me/example"},
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="v0")],
        )
        plan = JobOrchestrator(DummyRepository(), PlanningPolicy()).plan(job)
        params = plan.steps[0].params
        self.assertEqual(params["caption_template"], "看这里")
        self.assertEqual(
            params["caption_buttons"],
            (("打开频道", "https://t.me/example"),),
        )

    def test_unknown_template_variable_is_ignored_without_crashing(self) -> None:
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            policy={"caption_template": "{bogus}"},
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="v0")],
        )
        plan = JobOrchestrator(DummyRepository(), PlanningPolicy()).plan(job)
        self.assertNotIn("caption_template", plan.steps[0].params)
        self.assertNotIn("caption_buttons", plan.steps[0].params)

    def test_thumbnail_path_is_frozen_into_steps(self) -> None:
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            policy={"thumbnail_path": "/data/content/thumbnail-42.jpg"},
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="v0")],
        )
        plan = JobOrchestrator(DummyRepository(), PlanningPolicy()).plan(job)
        self.assertTrue(
            all(
                step.params.get("thumbnail_path") == "/data/content/thumbnail-42.jpg"
                for step in plan.steps
            )
        )

    def test_audio_is_planned_as_its_own_document_step(self) -> None:
        job = Job(
            owner_id=42,
            destination="@destination",
            state=JobState.ANALYZED,
            policy={},
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.AUDIO,
                    source="url:https://example.test/a",
                    metadata={"send_as_document_candidate": True},
                )
            ],
        )
        plan = JobOrchestrator(
            DummyRepository(),
            PlanningPolicy(cover_mode=False),
        ).plan(job)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].kind, PublishStepKind.CHANNEL_DOCUMENT)
        self.assertEqual(plan.steps[0].params["strategies"], {"0": "document"})
        self.assertEqual(plan.summary["audios"], 1)

