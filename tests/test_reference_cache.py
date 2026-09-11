from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.orchestrator import JobOrchestrator
from tgvio.application.reference_cache import TelegramReferenceEnricher
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class TelegramReferenceCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_reference_cache_survives_repository_round_trip_and_enriches_job(self) -> None:
        await self.repo.upsert_telegram_reference(
            "sha-photo",
            "@channel",
            MediaKind.PHOTO,
            "telegram:-100123:456",
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.ANALYZED,
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.PHOTO,
                    source="fixture:0",
                    sha256="sha-photo",
                )
            ],
        )
        await self.repo.create(job)
        enriched = await TelegramReferenceEnricher(self.repo).enrich(job)
        self.assertEqual(enriched.items[0].telegram_ref, "telegram:-100123:456")
        self.assertTrue(enriched.items[0].metadata["telegram_reference_cache_hit"])
        loaded = await self.repo.get(job.id)
        assert loaded is not None
        self.assertEqual(loaded.items[0].telegram_ref, "telegram:-100123:456")

        plan = JobOrchestrator(self.repo).plan(loaded)
        self.assertEqual(plan.steps[0].params["strategies"]["0"], "reuse_reference")

    async def test_reference_cache_is_scoped_by_destination_and_media_kind(self) -> None:
        await self.repo.upsert_telegram_reference(
            "same-sha",
            "@channel-a",
            MediaKind.VIDEO,
            "telegram:-1001:11",
        )
        self.assertEqual(
            await self.repo.get_telegram_reference(
                "same-sha", "@channel-a", MediaKind.VIDEO
            ),
            "telegram:-1001:11",
        )
        self.assertIsNone(
            await self.repo.get_telegram_reference(
                "same-sha", "@channel-b", MediaKind.VIDEO
            )
        )
        self.assertIsNone(
            await self.repo.get_telegram_reference(
                "same-sha", "@channel-a", MediaKind.PHOTO
            )
        )

