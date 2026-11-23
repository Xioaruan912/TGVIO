from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from telethon import events

from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime
from tgvio.application.intake import IncomingMedia, IntakeAcceptResult
from tgvio.domain.intake import SpoilerMode
from tgvio.domain.job import Job, MediaItem, MediaKind


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **_kwargs):
        self.messages.append((int(chat_id), str(text)))


class RecordingIntake:
    def __init__(self, *, style_json=None, spoiler_mode=SpoilerMode.SOURCE) -> None:
        self.batches: list[list[IncomingMedia]] = []
        self.policies: list[dict] = []
        self.spoiler_modes: list[SpoilerMode] = []
        self.preference = SimpleNamespace(style_json=style_json, spoiler_mode=spoiler_mode)

    async def get_user_preference(self, owner_id):
        return self.preference

    async def accept_once(self, *, owner_id, destination, media, policy, spoiler_mode):
        batch = list(media)
        self.batches.append(batch)
        self.policies.append(dict(policy))
        self.spoiler_modes.append(spoiler_mode)
        job = Job(
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
            policy=dict(policy),
        )
        return IntakeAcceptResult(job=job, created=True)


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
    def _runtime(self, *, window_ms=40, max_wait_ms=120, max_items=100, intake=None, url_enabled=False):
        client = FakeClient()
        intake = intake or RecordingIntake()
        settings = SimpleNamespace(
            worker_concurrency=2,
            batch_window_ms=window_ms,
            batch_max_wait_ms=max_wait_ms,
            batch_max_items=max_items,
            destination="@channel",
            allowed_users=(42,),
            url_enabled=url_enabled,
        )
        runtime = TelethonIntakeRuntime(
            client,
            settings,
            intake,
            PassThroughRunner(),
            console_url="https://console.example",
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

    async def test_forwarded_batch_gets_one_reply_that_points_to_the_console(self) -> None:
        runtime, _intake, client = self._runtime(window_ms=0)
        await runtime._queue_batch(42, 42, [media(30), media(31)])
        await runtime.stop()
        self.assertEqual(len(client.messages), 1)
        self.assertIn("2 个媒体", client.messages[0][1])
        self.assertIn("https://console.example", client.messages[0][1])

    async def test_console_picks_are_silent_and_carry_their_options(self) -> None:
        runtime, intake, client = self._runtime(window_ms=0)
        await runtime.accept_source_media(
            42, [media(40)], "clips", True, {"archive_only": True, "spoiler_mode": SpoilerMode.ALWAYS_SPOILER},
        )
        await runtime.stop()
        self.assertEqual(client.messages, [])
        self.assertEqual(intake.policies[0], {"source_label": "clips", "merge_album": True, "archive_only": True})
        self.assertEqual(intake.spoiler_modes, [SpoilerMode.ALWAYS_SPOILER])

    async def test_the_chosen_publish_style_applies_to_every_new_job(self) -> None:
        intake = RecordingIntake(style_json='{"style": "minimal"}')
        runtime, intake, _client = self._runtime(window_ms=0, intake=intake)
        await runtime._queue_batch(42, 42, [media(50)])
        await runtime.stop()
        self.assertEqual(intake.policies[0]["publish_style"], {"cover_mode": False, "forward_caption": False})

    async def test_no_chosen_style_leaves_the_configured_baseline_and_ask_mode_is_gone(self) -> None:
        intake = RecordingIntake(spoiler_mode=SpoilerMode.ASK)
        runtime, intake, _client = self._runtime(window_ms=0, intake=intake)
        await runtime._queue_batch(42, 42, [media(60)])
        await runtime.stop()
        self.assertNotIn("publish_style", intake.policies[0])
        self.assertEqual(intake.spoiler_modes, [SpoilerMode.SOURCE])

    async def test_manual_begin_end_collects_multiple_messages_and_captions_into_one_job(self) -> None:
        runtime, intake, client = self._runtime(window_ms=0)

        def cmd_event(text: str):
            return SimpleNamespace(
                chat_id=42,
                sender_id=42,
                raw_text=text,
                message=SimpleNamespace(id=1, raw_text=text, media=None, photo=None, video=None, document=None),
            )

        def media_event(msg_id: int, caption: str = ""):
            return SimpleNamespace(
                chat_id=42,
                sender_id=42,
                raw_text=caption,
                message=SimpleNamespace(
                    id=msg_id,
                    message=caption,
                    raw_text=caption,
                    grouped_id=None,
                    photo=SimpleNamespace(sizes=[]),
                    video=None,
                    document=None,
                    media=SimpleNamespace(spoiler=False),
                    file=SimpleNamespace(size=1024, name=f"p{msg_id}.jpg", mime_type="image/jpeg", ext=".jpg"),
                ),
            )

        # Before /begin, forwarding does NOT create a job
        await runtime._on_message(media_event(100))
        self.assertEqual(intake.batches, [])
        self.assertEqual(len(client.messages), 1)
        self.assertIn("/begin", client.messages[0][1])

        # /begin starts manual collection
        with self.assertRaises(events.StopPropagation):
            await runtime._on_message(cmd_event("/begin"))

        # Forward two separate messages + one text caption -> still no job created yet
        await runtime._on_message(media_event(101))
        await runtime._on_message(media_event(102))
        await runtime._on_message(cmd_event("合集标题"))
        self.assertEqual(intake.batches, [])

        # /end commits all collected items into ONE job with caption attached
        with self.assertRaises(events.StopPropagation):
            await runtime._on_message(cmd_event("/end"))
        self.assertEqual(len(intake.batches), 1)
        self.assertEqual([item.source_message_id for item in intake.batches[0]], [101, 102])
        self.assertEqual(intake.batches[0][0].caption, "合集标题")
        await runtime.stop()

    async def test_manual_cancel_discards_buffered_items(self) -> None:
        runtime, intake, client = self._runtime(window_ms=0)

        def cmd_event(text: str):
            return SimpleNamespace(
                chat_id=42,
                sender_id=42,
                raw_text=text,
                message=SimpleNamespace(id=1, raw_text=text, media=None, photo=None, video=None, document=None),
            )

        def media_event(msg_id: int):
            return SimpleNamespace(
                chat_id=42,
                sender_id=42,
                raw_text="",
                message=SimpleNamespace(
                    id=msg_id,
                    message="",
                    raw_text="",
                    grouped_id=None,
                    photo=SimpleNamespace(sizes=[]),
                    video=None,
                    document=None,
                    media=SimpleNamespace(spoiler=False),
                    file=SimpleNamespace(size=512, name=f"p{msg_id}.jpg", mime_type="image/jpeg", ext=".jpg"),
                ),
            )

        with self.assertRaises(events.StopPropagation):
            await runtime._on_message(cmd_event("/begin"))
        await runtime._on_message(media_event(201))
        with self.assertRaises(events.StopPropagation):
            await runtime._on_message(cmd_event("/cancel"))
        self.assertEqual(intake.batches, [])
        self.assertIn("已取消本次收集", client.messages[-1][1])
        await runtime.stop()

    async def test_manual_undo_end_archive_and_persistence_across_restart(self) -> None:
        flags_store: dict[str, str] = {}

        class PersistRepo:
            async def get_runtime_flags(self):
                return dict(flags_store)

            async def set_runtime_flag(self, key: str, value: str):
                flags_store[key] = value

            async def job_state_counts(self):
                return {"downloading": 1, "failed": 0}

            async def archive_state_counts(self):
                return {"committed": 95, "failed": 0}

            async def get_accepted_order(self, job_id: str):
                return 1

        class RepoRunner(PassThroughRunner):
            repository = PersistRepo()

        client = FakeClient()
        intake = RecordingIntake()
        settings = SimpleNamespace(
            worker_concurrency=2,
            batch_window_ms=0,
            batch_max_wait_ms=1,
            batch_max_items=100,
            destination="@channel",
            allowed_users=(42,),
            url_enabled=False,
        )
        rt1 = TelethonIntakeRuntime(client, settings, intake, RepoRunner(), console_url="https://ds.example")

        def cmd_event(text: str):
            return SimpleNamespace(
                chat_id=42, sender_id=42, raw_text=text,
                message=SimpleNamespace(id=1, message=text, raw_text=text, media=None, photo=None, video=None, document=None),
            )

        def media_event(msg_id: int):
            return SimpleNamespace(
                chat_id=42, sender_id=42, raw_text="",
                message=SimpleNamespace(
                    id=msg_id, message="", raw_text="", grouped_id=None,
                    photo=SimpleNamespace(sizes=[]), video=None, document=None,
                    media=SimpleNamespace(spoiler=False),
                    file=SimpleNamespace(size=512, name=f"p{msg_id}.jpg", mime_type="image/jpeg", ext=".jpg"),
                ),
            )

        with self.assertRaises(events.StopPropagation):
            await rt1._on_message(cmd_event("/begin"))
        await rt1._on_message(media_event(301))
        await rt1._on_message(media_event(302))
        with self.assertRaises(events.StopPropagation):
            await rt1._on_message(cmd_event("/undo"))
        self.assertIn("已撤回最后 1 个媒体", client.messages[-1][1])
        await rt1.stop()

        # Simulate container restart: new runtime restores persisted /begin session!
        rt2 = TelethonIntakeRuntime(client, settings, intake, RepoRunner(), console_url="https://ds.example")
        await rt2.start()
        with self.assertRaises(events.StopPropagation):
            await rt2._on_message(cmd_event("/status"))
        self.assertIn("已暂存 1 个媒体", client.messages[-1][1])
        with self.assertRaises(events.StopPropagation):
            await rt2._on_message(cmd_event("/end_archive"))
        self.assertEqual(len(intake.batches), 1)
        self.assertEqual([i.source_message_id for i in intake.batches[0]], [301])
        self.assertTrue(intake.policies[0].get("archive_only"))
        await rt2.stop()


if __name__ == "__main__":
    unittest.main()
