from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.result_card import ResultCardService, public_post_link
from tgvio.domain.job import (
    DOWNLOAD_SKIPPED_CODE_KEY,
    DOWNLOAD_SKIPPED_KEY,
    Job,
    JobState,
    MediaItem,
    MediaKind,
)
from tgvio.domain.operations import RevocationState
from tgvio.domain.publish import PublishEffect, PublishPlan
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class PublicPostLinkTests(unittest.TestCase):
    def test_public_username_builds_link(self) -> None:
        url, reason = public_post_link("@mychannel", ["55"])
        self.assertEqual(url, "https://t.me/mychannel/55")
        self.assertIsNone(reason)

    def test_numeric_destination_link_is_not_guessed(self) -> None:
        url, reason = public_post_link("-1001234567890", ["55"])
        self.assertIsNone(url)
        self.assertIsNotNone(reason)

    def test_missing_effect_message_has_no_link(self) -> None:
        url, reason = public_post_link("@mychannel", [])
        self.assertIsNone(url)
        self.assertIsNotNone(reason)


class FavoritesAndResultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _job(self, job_id: str, *, owner_id: int = 7, destination: str = "@mychannel") -> Job:
        job = Job(owner_id=owner_id, destination=destination, items=[], id=job_id)
        await self.repo.create(job)
        return job

    async def test_favorites_are_owner_scoped_and_idempotent(self) -> None:
        await self._job("j1")
        await self._job("foreign", owner_id=9)

        self.assertTrue(await self.repo.add_favorite(7, "j1"))
        self.assertFalse(await self.repo.add_favorite(7, "j1"))
        self.assertTrue(await self.repo.is_favorite(7, "j1"))
        self.assertFalse(await self.repo.is_favorite(9, "j1"))
        self.assertFalse(await self.repo.add_favorite(7, "foreign"))
        self.assertFalse(await self.repo.add_favorite(9, "missing"))

        self.assertEqual(await self.repo.count_favorites(7), 1)
        jobs = await self.repo.list_favorite_jobs(7)
        self.assertEqual([item.id for item in jobs], ["j1"])
        self.assertEqual(await self.repo.favorite_job_ids(7, ("j1", "foreign")), {"j1"})

        self.assertTrue(await self.repo.remove_favorite(7, "j1"))
        self.assertFalse(await self.repo.is_favorite(7, "j1"))
        self.assertEqual(await self.repo.count_favorites(7), 0)

    async def test_favorites_survive_hidden_history(self) -> None:
        job = await self._job("j1")
        await self.repo.add_favorite(7, "j1")
        await self.repo.hide_jobs(("j1",), reason="daily_rollover", now=1.0)
        self.assertTrue(await self.repo.is_favorite(7, "j1"))
        jobs = await self.repo.list_favorite_jobs(7)
        self.assertEqual([item.id for item in jobs], ["j1"])
        self.assertIsNotNone(await self.repo.get("j1"))

    async def test_quiet_mode_and_style_preferences_round_trip(self) -> None:
        pref = await self.repo.get_user_preference(7)
        self.assertFalse(pref.quiet_mode)
        self.assertIsNone(pref.style_json)

        await self.repo.set_user_quiet_mode(7, True)
        await self.repo.set_user_style(7, '{"cover_mode": true}')
        pref = await self.repo.get_user_preference(7)
        self.assertTrue(pref.quiet_mode)
        self.assertEqual(pref.style_json, '{"cover_mode": true}')

        await self.repo.set_user_quiet_mode(7, False)
        self.assertFalse((await self.repo.get_user_preference(7)).quiet_mode)

    async def test_result_card_reports_telegram_and_archive_independently(self) -> None:
        job = await self._job("j1")
        plan = PublishPlan(job_id="j1", steps=(), summary={})
        await self.repo.save_publish_plan(plan)
        await self.repo.record_publish_effects(
            (
                PublishEffect(
                    plan_id=plan.id,
                    step_index=0,
                    effect_type="telegram_channel_message",
                    external_chat_id="-100123",
                    external_message_id="55",
                    detail={},
                ),
                PublishEffect(
                    plan_id=plan.id,
                    step_index=1,
                    effect_type="publish_step_receipts_committed",
                    external_chat_id="0",
                    external_message_id="0",
                    detail={},
                ),
            )
        )
        job.state = JobState.SUCCEEDED
        await self.repo.save(job)

        card = await ResultCardService(self.repo).build(job, favorited=True)
        self.assertEqual(card.telegram_state, "succeeded")
        self.assertEqual(card.confirmed_messages, 1)
        self.assertEqual(card.link_url, "https://t.me/mychannel/55")
        self.assertIsNone(card.archive_state)
        self.assertTrue(card.favorited)
        from tgvio.domain.operations import RevocationState
        effects = await self.repo.list_publish_effects(plan.id)
        ids = tuple(e.id for e in effects if e.effect_type == "telegram_channel_message")
        await self.repo.ensure_publish_effect_revocations(job.id, ids)
        await self.repo.checkpoint_publish_effect_revocations(
            job.id, ids, state=RevocationState.DELETED,
        )
        revoked = await ResultCardService(self.repo).build(job)
        self.assertEqual(revoked.telegram_state, "revoked")
        self.assertIsNone(revoked.link_url)
        self.assertEqual(revoked.confirmed_messages, 0)

    async def test_result_card_counts_skipped_items(self) -> None:
        job = Job(
            owner_id=7,
            destination="@mychannel",
            items=[
                MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture:1"),
                MediaItem(
                    index=1,
                    kind=MediaKind.VIDEO,
                    source="fixture:2",
                    metadata={
                        DOWNLOAD_SKIPPED_KEY: True,
                        DOWNLOAD_SKIPPED_CODE_KEY: "telegram_file_timeout",
                    },
                ),
            ],
            id="j-skip",
        )
        await self.repo.create(job)
        plan = PublishPlan(job_id="j-skip", steps=(), summary={})
        await self.repo.save_publish_plan(plan)
        job.state = JobState.SUCCEEDED
        await self.repo.save(job)

        card = await ResultCardService(self.repo).build(job)
        self.assertEqual(card.skipped_items, 1)
        self.assertEqual(card.skipped_reason, "Telegram 取用该文件超时")

    async def test_result_card_lists_every_published_reference(self) -> None:
        job = await self._job("j3")
        plan = PublishPlan(job_id="j3", steps=(), summary={})
        await self.repo.save_publish_plan(plan)
        await self.repo.record_publish_effects(
            (
                PublishEffect(
                    plan_id=plan.id,
                    step_index=0,
                    effect_type="telegram_channel_message",
                    external_chat_id="-100123",
                    external_message_id="1375",
                    detail={},
                ),
                PublishEffect(
                    plan_id=plan.id,
                    step_index=1,
                    effect_type="telegram_discussion_message",
                    external_chat_id="-100999",
                    external_message_id="4244",
                    detail={},
                ),
                PublishEffect(
                    plan_id=plan.id,
                    step_index=1,
                    effect_type="telegram_discussion_message",
                    external_chat_id="-100999",
                    external_message_id="4245",
                    detail={},
                ),
                PublishEffect(
                    plan_id=plan.id,
                    step_index=1,
                    effect_type="telegram_discussion_message",
                    external_chat_id="-100999",
                    external_message_id="4245",
                    detail={},
                ),
            )
        )
        job.state = JobState.SUCCEEDED
        await self.repo.save(job)
        card = await ResultCardService(self.repo).build(job)
        self.assertEqual(card.channel_message_ids, ("1375",))
        self.assertEqual(card.discussion_message_ids, ("4244", "4245"))
        self.assertEqual(card.discussion_range, "4244–4245")

    async def test_result_card_marks_uncertain_publish(self) -> None:
        job = await self._job("j2", destination="@mychannel")
        job.state = JobState.FAILED
        job.error_code = "publish_uncertain"
        await self.repo.save(job)
        card = await ResultCardService(self.repo).build(job)
        self.assertEqual(card.telegram_state, "uncertain")
        self.assertIsNone(card.link_url)


if __name__ == "__main__":
    unittest.main()


class PublishStyleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / 'state.sqlite3')
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_named_custom_and_reset_style(self) -> None:
        from tgvio.application.publish_styles import PublishStyleService, resolve_style

        self.assertEqual(resolve_style(None)[0], 'cover')
        service = PublishStyleService(self.repo)
        self.assertTrue((await service.current(7))['cover_mode'])

        await service.set_named(7, 'minimal')
        policy = await service.current(7)
        self.assertFalse(policy['cover_mode'])
        self.assertFalse(policy['forward_caption'])

        await service.set_custom(7, policy={'cover_mode': True, 'forward_caption': True})
        policy = await service.current(7)
        self.assertTrue(policy['cover_mode'])
        self.assertTrue(policy['forward_caption'])

        await service.reset(7)
        policy = await service.current(7)
        self.assertTrue(policy['cover_mode'])
        self.assertFalse(policy['forward_caption'])

    async def test_style_snapshot_overrides_global_planning_policy(self) -> None:
        from tgvio.application.orchestrator import JobOrchestrator, PlanningPolicy
        from tgvio.domain.job import JobState, MediaItem, MediaKind
        from tgvio.domain.publish import PublishStepKind

        items = [
            MediaItem(index=0, kind=MediaKind.PHOTO, source='x'),
            MediaItem(index=1, kind=MediaKind.VIDEO, source='y'),
        ]
        job = Job(owner_id=7, destination='@channel', items=items, id='j1', state=JobState.ANALYZED)
        await self.repo.create(job)
        orchestrator = JobOrchestrator(self.repo, PlanningPolicy(cover_mode=True, forward_caption=True))

        global_plan = orchestrator.plan(job)
        self.assertTrue(any(step.kind == PublishStepKind.CHANNEL_COVER_ALBUM for step in global_plan.steps))

        job.policy = {'publish_style': {'cover_mode': False, 'forward_caption': False}}
        minimal_plan = orchestrator.plan(job)
        self.assertFalse(any(step.kind == PublishStepKind.CHANNEL_COVER_ALBUM for step in minimal_plan.steps))
        self.assertFalse(any(step.target.value == 'discussion' for step in minimal_plan.steps))
        for step in minimal_plan.steps:
            self.assertFalse(step.params['forward_caption'])


class ResultCardIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _job_with_effects(self, effects, *, destination="@mychannel"):
        job = Job(owner_id=7, destination=destination, items=[], id="j1")
        await self.repo.create(job)
        plan = PublishPlan(job_id="j1", steps=(), summary={})
        await self.repo.save_publish_plan(plan)
        await self.repo.record_publish_effects(
            tuple(
                PublishEffect(
                    plan_id=plan.id,
                    step_index=index,
                    effect_type=kind,
                    external_chat_id="-100123",
                    external_message_id=str(message_id),
                    detail={},
                )
                for index, (kind, message_id) in enumerate(effects)
            )
        )
        job.state = JobState.SUCCEEDED
        await self.repo.save(job)
        return job

    async def test_duplicate_effects_do_not_break_link(self) -> None:
        job = await self._job_with_effects(
            [
                ("telegram_channel_message", "55"),
                ("telegram_channel_message", "55"),
            ]
        )
        card = await ResultCardService(self.repo).build(job)
        self.assertEqual(card.link_url, "https://t.me/mychannel/55")
        self.assertEqual(card.telegram_state, "succeeded")

    async def test_revoked_channel_cover_removes_link_and_marks_partial(self) -> None:
        job = await self._job_with_effects(
            [
                ("telegram_channel_message", "55"),
                ("telegram_discussion_message", "77"),
            ]
        )
        plan = await self.repo.get_publish_plan(job.id)
        assert plan is not None
        effects = await self.repo.list_publish_effects(plan.id)
        channel_effect = next(e for e in effects if e.effect_type == "telegram_channel_message")
        await self.repo.ensure_publish_effect_revocations(job.id, (channel_effect.id,))
        await self.repo.checkpoint_publish_effect_revocations(
            job.id, (channel_effect.id,), state=RevocationState.DELETED
        )
        card = await ResultCardService(self.repo).build(job)
        self.assertEqual(card.telegram_state, "partially_revoked")
        self.assertIsNone(card.link_url)

    async def test_public_post_link_dedupes_and_requires_username(self) -> None:
        self.assertEqual(
            public_post_link("@c", ["55", "55"]), ("https://t.me/c/55", None)
        )
        url, reason = public_post_link("-100123", ["55"])
        self.assertIsNone(url)
        self.assertIsNotNone(reason)
