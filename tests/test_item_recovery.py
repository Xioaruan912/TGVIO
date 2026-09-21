from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.item_recovery import (
    SkippedItemRecoveryService,
    SkippedItemRecoveryUnavailableError,
)
from tgvio.domain.intake import IntakeEventKey
from tgvio.domain.job import (
    DOWNLOAD_SKIPPED_CODE_KEY,
    DOWNLOAD_SKIPPED_KEY,
    Job,
    JobState,
    MediaItem,
    MediaKind,
)
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class SkippedItemRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.service = SkippedItemRecoveryService(self.repo)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_migration_creates_parent_child_mapping(self) -> None:
        self.assertEqual(self.repo.schema_status()["user_version"], 18)
        cursor = await self.repo._require().execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='item_recovery_jobs'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        self.assertIsNotNone(row)

    async def test_clones_only_skipped_items_and_resets_derived_state(self) -> None:
        parent = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            policy={
                "cover_mode": True,
                "forward_caption": False,
                "caption_template": "#{index}",
                "download_skipped": [{"index": 1, "error_code": "telegram_file_timeout"}],
            },
            items=[
                MediaItem(index=0, kind=MediaKind.PHOTO, source="telegram:1:10"),
                MediaItem(
                    index=1,
                    kind=MediaKind.VIDEO,
                    source="telegram:1:11",
                    caption="frozen caption",
                    local_path="/downloads/parent/1.mp4",
                    name="source.mp4",
                    size_bytes=123,
                    mime_type="video/mp4",
                    width=1920,
                    height=1080,
                    duration_seconds=7.5,
                    container="mov,mp4",
                    codec="h264",
                    spoiler=True,
                    grouped_id=99,
                    source_chat_id=1,
                    source_message_id=11,
                    sha256="a" * 64,
                    telegram_ref="cached-reference",
                    metadata={
                        "source_type": "user_source",
                        DOWNLOAD_SKIPPED_KEY: True,
                        DOWNLOAD_SKIPPED_CODE_KEY: "telegram_file_timeout",
                        "canonical_path": "/downloads/parent/1.remux.mp4",
                        "faststart": True,
                    },
                ),
            ],
        )
        await self.repo.create(parent)

        child = await self.service.recover(parent_job_id=parent.id, owner_id=42)

        self.assertEqual(child.state, JobState.RECEIVED)
        self.assertEqual(child.owner_id, parent.owner_id)
        self.assertEqual(child.destination, parent.destination)
        self.assertEqual(
            child.policy,
            {"cover_mode": True, "forward_caption": False, "caption_template": "#{index}"},
        )
        self.assertEqual(len(child.items), 1)
        item = child.items[0]
        self.assertEqual(item.index, 0)
        self.assertEqual(item.source, "telegram:1:11")
        self.assertEqual(item.caption, "frozen caption")
        self.assertEqual(item.name, "source.mp4")
        self.assertEqual(item.size_bytes, 123)
        self.assertTrue(item.spoiler)
        self.assertEqual((item.grouped_id, item.source_chat_id, item.source_message_id), (99, 1, 11))
        self.assertIsNone(item.local_path)
        self.assertIsNone(item.sha256)
        self.assertIsNone(item.telegram_ref)
        self.assertIsNone(item.mime_type)
        self.assertIsNone(item.width)
        self.assertIsNone(item.height)
        self.assertIsNone(item.duration_seconds)
        self.assertIsNone(item.container)
        self.assertIsNone(item.codec)
        self.assertEqual(item.metadata, {"source_type": "user_source"})

    async def test_duplicate_requests_return_the_existing_child(self) -> None:
        parent = await self._terminal_parent_with_skipped_item()
        first = await self.service.recover(parent_job_id=parent.id, owner_id=42)
        second = await self.service.recover(parent_job_id=parent.id, owner_id=42)

        self.assertEqual(first.id, second.id)
        cursor = await self.repo._require().execute("SELECT COUNT(*) AS count FROM item_recovery_jobs")
        row = await cursor.fetchone()
        await cursor.close()
        self.assertEqual(row["count"], 1)

    async def test_owner_mismatch_refuses_without_creating_a_child(self) -> None:
        parent = await self._terminal_parent_with_skipped_item()

        with self.assertRaises(SkippedItemRecoveryUnavailableError):
            await self.service.recover(parent_job_id=parent.id, owner_id=99)

        self.assertIsNone(await self.repo.get_by_accepted_order(42, 2))

    async def test_refuses_nonterminal_or_parent_without_skipped_items(self) -> None:
        active = Job(
            owner_id=42,
            destination="@channel",
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.VIDEO,
                    source="telegram:1:12",
                    metadata={DOWNLOAD_SKIPPED_KEY: True},
                )
            ],
        )
        await self.repo.create(active)
        with self.assertRaises(SkippedItemRecoveryUnavailableError):
            await self.service.recover(parent_job_id=active.id, owner_id=42)

        complete = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="telegram:1:13")],
        )
        await self.repo.create(complete)
        with self.assertRaises(SkippedItemRecoveryUnavailableError):
            await self.service.recover(parent_job_id=complete.id, owner_id=42)

    async def test_source_intake_keys_remain_owned_by_the_parent(self) -> None:
        parent = await IntakeService(self.repo).accept(
            owner_id=42,
            destination="@channel",
            media=[
                IncomingMedia(
                    kind=MediaKind.VIDEO,
                    source="telegram:1:14",
                    source_chat_id=1,
                    source_message_id=14,
                )
            ],
        )
        parent.items = [
            replace(
                parent.items[0],
                metadata={DOWNLOAD_SKIPPED_KEY: True, DOWNLOAD_SKIPPED_CODE_KEY: "telegram_file_timeout"},
            )
        ]
        await self.repo.save(parent)
        await self.repo.transition(parent.id, JobState.FAILED, event_type="download_failed")

        child = await self.service.recover(parent_job_id=parent.id, owner_id=42)

        owners = await self.repo.lookup_intake_events((IntakeEventKey(1, 14),))
        self.assertEqual(owners, {IntakeEventKey(1, 14): parent.id})
        cursor = await self.repo._require().execute(
            "SELECT COUNT(*) AS count FROM intake_events WHERE job_id=?",
            (child.id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        self.assertEqual(row["count"], 0)

    async def _terminal_parent_with_skipped_item(self) -> Job:
        parent = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            items=[
                MediaItem(
                    index=4,
                    kind=MediaKind.VIDEO,
                    source="telegram:1:15",
                    metadata={DOWNLOAD_SKIPPED_KEY: True, DOWNLOAD_SKIPPED_CODE_KEY: "telegram_file_timeout"},
                )
            ],
        )
        await self.repo.create(parent)
        return parent
