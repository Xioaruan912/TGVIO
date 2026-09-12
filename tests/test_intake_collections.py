from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.domain.intake import JobDisplayMessage, SpoilerMode
from tgvio.domain.job import MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


def incoming(message_id: int, *, caption: str = "", spoiler: bool = False) -> IncomingMedia:
    return IncomingMedia(
        kind=MediaKind.VIDEO if message_id % 2 else MediaKind.PHOTO,
        source=f"telegram:42:{message_id}",
        caption=caption,
        size_bytes=1000 + message_id,
        name=f"media-{message_id}.mp4",
        spoiler=spoiler,
        grouped_id=900 if message_id in {1, 2} else None,
        source_chat_id=42,
        source_message_id=message_id,
        metadata={"fixture": True},
    )


class IntakeCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.database = Path(self.tmp.name) / "state.sqlite3"
        self.repo = SQLiteJobRepository(self.database)
        await self.repo.open()
        self.intake = IntakeService(self.repo)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_duplicate_update_returns_existing_job_without_second_create(self) -> None:
        first = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=[incoming(1)],
        )
        replay = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=[incoming(1)],
        )

        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(replay.job.id, first.job.id)
        self.assertEqual(len(await self.repo.list_recent(limit=10)), 1)
        events = await self.repo.lookup_intake_events(
            (self.intake._event_key(incoming(1)),)  # type: ignore[arg-type]
        )
        self.assertEqual(set(events.values()), {first.job.id})

    async def test_cross_connection_concurrent_replay_still_creates_one_job(self) -> None:
        second_repo = SQLiteJobRepository(self.database)
        await second_repo.open()
        try:
            second_intake = IntakeService(second_repo)

            async def accept(service: IntakeService):
                return await service.accept_once(
                    owner_id=7,
                    destination="@channel",
                    media=[incoming(15)],
                )

            first, second = await asyncio.gather(
                accept(self.intake),
                accept(second_intake),
            )
            self.assertEqual(int(first.created) + int(second.created), 1)
            self.assertEqual(first.job.id, second.job.id)
            self.assertEqual(len(await self.repo.list_recent(limit=10)), 1)
        finally:
            await second_repo.close()

    async def test_partial_overlap_creates_only_previously_unseen_media(self) -> None:
        first = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=[incoming(1), incoming(2)],
        )
        second = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=[incoming(2), incoming(3)],
        )

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertEqual([item.source_message_id for item in second.job.items], [3])
        jobs = await self.repo.list_recent(limit=10)
        self.assertEqual(len(jobs), 2)

    async def test_concurrent_replay_still_creates_exactly_one_job(self) -> None:
        async def accept():
            return await self.intake.accept_once(
                owner_id=7,
                destination="@channel",
                media=[incoming(10)],
            )

        results = await asyncio.gather(*(accept() for _ in range(8)))
        self.assertEqual(sum(result.created for result in results), 1)
        self.assertEqual(len({result.job.id for result in results}), 1)
        self.assertEqual(len(await self.repo.list_recent(limit=20)), 1)

    async def test_collection_survives_reopen_and_freezes_media_and_text_order(self) -> None:
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        self.assertEqual(await self.intake.add_collection_media(session, [incoming(1)]), 1)
        self.assertTrue(
            await self.intake.add_collection_text(
                session,
                text="第一行\n\n第二行",
                source_chat_id=42,
                source_message_id=100,
            )
        )

        await self.repo.close()
        self.repo = SQLiteJobRepository(self.database)
        await self.repo.open()
        self.intake = IntakeService(self.repo)
        reopened = await self.intake.open_collection(owner_id=7, chat_id=42)
        self.assertIsNotNone(reopened)
        assert reopened is not None
        self.assertEqual(reopened.id, session.id)
        self.assertEqual(await self.intake.add_collection_media(reopened, [incoming(2)]), 1)

        result = await self.intake.finalize_collection(
            owner_id=7,
            chat_id=42,
            destination="@channel",
            max_items=100,
            spoiler_mode=SpoilerMode.ALWAYS_NORMAL,
        )
        self.assertEqual(result.media_count, 2)
        self.assertEqual(result.text_count, 1)
        self.assertEqual(len(result.jobs), 1)
        job = result.jobs[0].job
        self.assertEqual([item.source_message_id for item in job.items], [1, 2])
        self.assertEqual(job.policy["collection_caption"], "第一行\n第二行")
        self.assertEqual(job.policy["collection_id"], session.id)
        self.assertEqual(job.policy["spoiler_decision"], "normal")
        self.assertTrue(job.policy["display_expected"])
        self.assertIsNone(await self.intake.open_collection(owner_id=7, chat_id=42))

    async def test_large_500_item_intake_dedupe_is_split_across_sql_bind_chunks(self) -> None:
        media = [incoming(1000 + index) for index in range(500)]
        first = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=media,
        )
        replay = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=media,
        )
        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(replay.job.id, first.job.id)
        self.assertEqual(len(first.job.items), 500)

    async def test_collection_entry_replay_is_idempotent_before_end(self) -> None:
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        self.assertEqual(await self.intake.add_collection_media(session, [incoming(1), incoming(2)]), 2)
        self.assertEqual(await self.intake.add_collection_media(session, [incoming(2), incoming(1)]), 0)
        media_count, text_count = await self.intake.collection_counts(session.id)
        self.assertEqual((media_count, text_count), (2, 0))

    async def test_collection_over_100_splits_jobs_without_dropping_overflow(self) -> None:
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        media = [incoming(index + 1) for index in range(105)]
        self.assertEqual(await self.intake.add_collection_media(session, media), 105)

        result = await self.intake.finalize_collection(
            owner_id=7,
            chat_id=42,
            destination="@channel",
            max_items=100,
            spoiler_mode=SpoilerMode.ALWAYS_NORMAL,
        )
        self.assertEqual([len(entry.job.items) for entry in result.jobs], [100, 5])
        flattened = [
            item.source_message_id
            for entry in result.jobs
            for item in entry.job.items
        ]
        self.assertEqual(flattened, list(range(1, 106)))
        self.assertEqual(result.jobs[0].job.policy["collection_part_count"], 2)
        self.assertEqual(result.jobs[1].job.policy["collection_part_index"], 1)

    async def test_spoiler_preferences_and_pending_decision_are_durable(self) -> None:
        default = await self.intake.get_user_preference(7)
        self.assertEqual(default.spoiler_mode, SpoilerMode.SOURCE)
        source = await self.intake.accept_once(
            owner_id=8,
            destination="@channel",
            media=[incoming(20, spoiler=True), incoming(21, spoiler=False)],
            spoiler_mode=SpoilerMode.SOURCE,
        )
        self.assertEqual(source.job.policy["spoiler_decision"], "source")
        self.assertEqual([item.spoiler for item in source.job.items], [True, False])

        stored = await self.intake.set_spoiler_mode(7, SpoilerMode.ASK)
        self.assertEqual(stored.spoiler_mode, SpoilerMode.ASK)

        pending = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=[incoming(1, spoiler=True), incoming(2, spoiler=False)],
            spoiler_mode=SpoilerMode.ASK,
            ask_timeout_seconds=60,
        )
        self.assertEqual(pending.job.policy["spoiler_decision"], "pending")
        self.assertIn("spoiler_deadline_epoch", pending.job.policy)

        decided = await self.intake.apply_spoiler_decision(pending.job, spoiler=True)
        self.assertEqual(decided.policy["spoiler_decision"], "spoiler")
        self.assertNotIn("spoiler_deadline_epoch", decided.policy)
        self.assertTrue(all(item.spoiler for item in decided.items))

        loaded = await self.repo.get(decided.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertTrue(all(item.spoiler for item in loaded.items))

    async def test_job_display_message_round_trips(self) -> None:
        accepted = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=[incoming(1)],
        )
        saved = await self.repo.save_job_display_message(
            JobDisplayMessage(
                job_id=accepted.job.id,
                chat_id=42,
                message_id=900,
            )
        )
        self.assertEqual(saved.message_id, 900)

        await self.repo.close()
        self.repo = SQLiteJobRepository(self.database)
        await self.repo.open()
        loaded = await self.repo.get_job_display_message(accepted.job.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual((loaded.chat_id, loaded.message_id), (42, 900))


if __name__ == "__main__":
    unittest.main()
