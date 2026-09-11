from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime
from tgvio.application.intake import IncomingMedia
from tgvio.domain.job import Job, MediaItem, MediaKind


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **_kwargs):
        self.messages.append((int(chat_id), str(text)))


class RecordingIntake:
    def __init__(self) -> None:
        self.batches: list[list[IncomingMedia]] = []

    async def accept(self, *, owner_id, destination, media):
        batch = list(media)
        self.batches.append(batch)
        return Job(
            owner_id=int(owner_id),
            destination=destination,
            items=[
                MediaItem(
                    index=index,
                    kind=item.kind,
                    source=item.source,
                    source_chat_id=item.source_chat_id,
                    source_message_id=item.source_message_id,
                )
                for index, item in enumerate(batch)
            ],
        )


class PassThroughRunner:
    async def process(self, job):
        return job


def media(message_id: int) -> IncomingMedia:
    return IncomingMedia(
        kind=MediaKind.PHOTO,
        source=f"telegram:42:{message_id}",
        source_chat_id=42,
        source_message_id=message_id,
    )


class IntakeBatchingTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, *, window_ms=40, max_wait_ms=120, max_items=100):
        client = FakeClient()
        intake = RecordingIntake()
        settings = SimpleNamespace(
            worker_concurrency=2,
            batch_window_ms=window_ms,
            batch_max_wait_ms=max_wait_ms,
            batch_max_items=max_items,
            destination="@channel",
            allowed_users=(42,),
        )
        runtime = TelethonIntakeRuntime(
            client,
            settings,
            intake,
            PassThroughRunner(),
        )
        return runtime, intake, client

    async def test_separate_album_events_within_window_become_one_job(self) -> None:
        runtime, intake, _client = self._runtime()
        await runtime._queue_batch(42, 42, [media(1), media(2)])
        await asyncio.sleep(0.01)
        await runtime._queue_batch(42, 42, [media(3), media(4)])
        await asyncio.sleep(0.07)
        self.assertEqual(len(intake.batches), 1)
        self.assertEqual(
            [item.source_message_id for item in intake.batches[0]],
            [1, 2, 3, 4],
        )
        await runtime.stop()

    async def test_max_items_splits_long_burst_without_dropping_media(self) -> None:
        runtime, intake, _client = self._runtime(max_items=3)
        await runtime._queue_batch(42, 42, [media(1), media(2), media(3), media(4), media(5)])
        await asyncio.sleep(0.07)
        self.assertEqual([len(batch) for batch in intake.batches], [3, 2])
        self.assertEqual(
            [item.source_message_id for batch in intake.batches for item in batch],
            [1, 2, 3, 4, 5],
        )
        await runtime.stop()

    async def test_shutdown_flushes_pending_media_instead_of_losing_it(self) -> None:
        runtime, intake, _client = self._runtime(window_ms=500, max_wait_ms=1000)
        await runtime._queue_batch(42, 42, [media(10), media(11)])
        self.assertEqual(intake.batches, [])
        await runtime.stop()
        self.assertEqual(len(intake.batches), 1)
        self.assertEqual(
            [item.source_message_id for item in intake.batches[0]],
            [10, 11],
        )

    async def test_zero_window_preserves_immediate_single_job_behavior(self) -> None:
        runtime, intake, _client = self._runtime(window_ms=0, max_wait_ms=1)
        await runtime._queue_batch(42, 42, [media(20)])
        self.assertEqual(len(intake.batches), 1)
        await runtime.stop()

