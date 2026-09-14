from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.result_card import ResultCardService, public_post_link
from tgvio.domain.job import Job, JobState
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
