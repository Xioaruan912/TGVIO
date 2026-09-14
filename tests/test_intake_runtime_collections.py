from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import unittest

from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime
from tgvio.application.collection_editing import CollectionEditingService
from tgvio.application.intake import IncomingMedia, IntakeService
from tgvio.application.operation_tokens import OperationTokenService
from tgvio.domain.intake import JobDisplayMessage, SpoilerMode
from tgvio.domain.job import JobState, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class FakeClient:
    def __init__(self) -> None:
        self.handlers: list[tuple[object, object]] = []
        self.sent: list[dict[str, object]] = []
        self.edits: list[dict[str, object]] = []
        self.fail_edit_ids: set[int] = set()
        self._next_id = 1000

    def add_event_handler(self, handler, event) -> None:
        self.handlers.append((handler, event))

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        message = SimpleNamespace(id=self._next_id)
        self.sent.append(
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "buttons": kwargs.get("buttons"),
                "id": message.id,
            }
        )
        return message

    async def edit_message(self, chat_id, message_id, text, **kwargs):
        self.edits.append(
            {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "text": str(text),
                "buttons": kwargs.get("buttons"),
            }
        )
        if int(message_id) in self.fail_edit_ids:
            raise RuntimeError("message missing")
        return SimpleNamespace(id=int(message_id))


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def process(self, job):
        self.calls.append(job.id)
        return job


class FakeEvent:
    def __init__(
        self,
        *,
        chat_id: int = 42,
        sender_id: int = 7,
        raw_text: str = "",
        message=None,
        data: bytes = b"",
    ) -> None:
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = raw_text
        self.message = message or SimpleNamespace(
            id=999,
            photo=None,
            video=None,
            document=None,
            message=raw_text,
            grouped_id=None,
        )
        self.data = data
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[tuple[str, object]] = []

    async def answer(self, text="", alert=False):
        self.answers.append((str(text), bool(alert)))

    async def edit(self, text, **kwargs):
        self.edits.append((str(text), kwargs.get("buttons")))


def settings(**overrides):
    values = {
        "worker_concurrency": 2,
        "batch_window_ms": 0,
        "batch_max_wait_ms": 1,
        "batch_max_items": 100,
        "destination": "@channel",
        "allowed_users": (7,),
        "url_enabled": True,
        "collections_enabled": True,
        "collection_preview_enabled": False,
        "spoiler_confirm_timeout_seconds": 60,
        "publish_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def media_message(message_id: int, *, caption: str = "caption"):
    return SimpleNamespace(
        id=message_id,
        photo=None,
        video=object(),
        document=object(),
        file=SimpleNamespace(name=f"video-{message_id}.mp4", ext=".mp4", size=1234),
        media=SimpleNamespace(spoiler=False),
        message=caption,
        grouped_id=None,
    )


def incoming(message_id: int) -> IncomingMedia:
    return IncomingMedia(
        kind=MediaKind.VIDEO,
        source=f"telegram:42:{message_id}",
        source_chat_id=42,
        source_message_id=message_id,
    )


class IntakeRuntimeCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.database = Path(self.tmp.name) / "state.sqlite3"
        self.repo = SQLiteJobRepository(self.database)
        await self.repo.open()
        self.intake = IntakeService(self.repo)
        self.client = FakeClient()
        self.runner = RecordingRunner()
        self.runtime = TelethonIntakeRuntime(
            self.client,
            settings(),
            self.intake,
            self.runner,
        )

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()
        await self.repo.close()
        self.tmp.cleanup()

    async def _drain(self) -> None:
        await asyncio.sleep(0.05)

    async def test_begin_media_text_end_creates_one_durable_collection_job(self) -> None:
        await self.runtime._begin_collection(42, 7)
        session = await self.intake.open_collection(owner_id=7, chat_id=42)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertIsNotNone(session.status_message_id)

        await self.runtime._on_message(
            FakeEvent(chat_id=42, sender_id=7, message=media_message(10))
        )
        await self.runtime._on_message(
            FakeEvent(chat_id=42, sender_id=7, raw_text="第一行\n第二行")
        )
        self.assertEqual(await self.intake.collection_counts(session.id), (1, 1))

        await self.runtime._end_collection(42, 7)
        await self._drain()

        self.assertIsNone(await self.intake.open_collection(owner_id=7, chat_id=42))
        jobs = await self.repo.list_recent(owner_id=7, limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].policy["collection_caption"], "第一行\n第二行")
        self.assertEqual(self.runner.calls, [jobs[0].id])
        ref = await self.repo.get_job_display_message(jobs[0].id)
        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.message_id, session.status_message_id)

    async def test_url_remains_immediate_while_collection_is_open(self) -> None:
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        message = SimpleNamespace(
            id=50,
            photo=None,
            video=None,
            document=None,
            message="https://example.com/watch?v=abc",
            grouped_id=None,
        )
        await self.runtime._on_message(
            FakeEvent(
                chat_id=42,
                sender_id=7,
                raw_text=message.message,
                message=message,
            )
        )
        await self._drain()

        self.assertEqual(await self.intake.collection_counts(session.id), (0, 0))
        jobs = await self.repo.list_recent(owner_id=7, limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].items[0].source.startswith("url:"))

    async def test_default_source_mode_preserves_original_spoiler(self) -> None:
        source = IncomingMedia(
            kind=MediaKind.VIDEO,
            source="telegram:42:69",
            source_chat_id=42,
            source_message_id=69,
            spoiler=True,
        )
        await self.runtime._accept_and_schedule(42, 7, [source])
        await self._drain()
        jobs = await self.repo.list_recent(owner_id=7, limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].policy["spoiler_decision"], "source")
        self.assertTrue(jobs[0].items[0].spoiler)

    async def test_duplicate_replay_does_not_send_second_status_or_schedule_twice(self) -> None:
        await self.runtime._accept_and_schedule(42, 7, [incoming(70)])
        await self._drain()
        sent_after_first = len(self.client.sent)
        calls_after_first = list(self.runner.calls)

        await self.runtime._accept_and_schedule(42, 7, [incoming(70)])
        await self._drain()

        self.assertEqual(len(self.client.sent), sent_after_first)
        self.assertEqual(self.runner.calls, calls_after_first)
        self.assertEqual(len(await self.repo.list_recent(owner_id=7, limit=10)), 1)

    async def test_ask_spoiler_choice_updates_items_then_schedules_once(self) -> None:
        await self.intake.set_spoiler_mode(7, SpoilerMode.ASK)
        await self.runtime._accept_and_schedule(42, 7, [incoming(80), incoming(81)])
        await self._drain()
        jobs = await self.repo.list_recent(owner_id=7, limit=10)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.policy["spoiler_decision"], "pending")
        self.assertEqual(self.runner.calls, [])

        event = FakeEvent(data=f"intake:spoiler:{job.id}:spoiler".encode())
        await self.runtime._on_intake_callback(event)
        await self._drain()

        loaded = await self.repo.get(job.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.policy["spoiler_decision"], "spoiler")
        self.assertTrue(all(item.spoiler for item in loaded.items))
        self.assertEqual(self.runner.calls, [job.id])

    async def test_ask_cancel_marks_job_cancelled_without_processing(self) -> None:
        await self.intake.set_spoiler_mode(7, SpoilerMode.ASK)
        await self.runtime._accept_and_schedule(42, 7, [incoming(90)])
        jobs = await self.repo.list_recent(owner_id=7, limit=10)
        job = jobs[0]

        event = FakeEvent(data=f"intake:spoiler:{job.id}:cancel".encode())
        await self.runtime._on_intake_callback(event)
        await self._drain()

        loaded = await self.repo.get(job.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.state, JobState.CANCELLED)
        self.assertEqual(self.runner.calls, [])

    async def test_ask_timeout_resolves_normal_and_processes(self) -> None:
        accepted = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=[incoming(100)],
            policy={"display_expected": True},
            spoiler_mode=SpoilerMode.ASK,
            ask_timeout_seconds=60,
        )
        accepted.job.policy["spoiler_deadline_epoch"] = int(time.time()) - 1
        await self.repo.save(accepted.job)
        await self.runtime._ensure_spoiler_confirmation(
            accepted.job,
            chat_id=42,
            status_message_id=None,
        )
        await self._drain()

        loaded = await self.repo.get(accepted.job.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.policy["spoiler_decision"], "normal")
        self.assertFalse(any(item.spoiler for item in loaded.items))
        self.assertEqual(self.runner.calls, [accepted.job.id])

    async def test_retrying_partial_collection_finalize_recovers_existing_chunk(self) -> None:
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        media = [incoming(index + 200) for index in range(105)]
        await self.intake.add_collection_media(session, media)
        first = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=media[:100],
            policy={
                "collection_id": session.id,
                "collection_part_index": 0,
                "collection_part_count": 2,
                "display_expected": True,
            },
            spoiler_mode=SpoilerMode.SOURCE,
        )
        self.assertTrue(first.created)
        self.assertEqual(self.runner.calls, [])
        self.runtime._settings.batch_max_items = 500

        await self.runtime._end_collection(42, 7)
        await self._drain()

        jobs = sorted(
            await self.repo.list_recent(owner_id=7, limit=10),
            key=lambda job: int(job.policy.get("collection_part_index", 0)),
        )
        self.assertEqual([len(job.items) for job in jobs], [100, 5])
        self.assertEqual(set(self.runner.calls), {job.id for job in jobs})
        self.assertEqual(len(self.runner.calls), 2)
        self.assertIsNone(await self.intake.open_collection(owner_id=7, chat_id=42))

    def test_all_intake_callback_payloads_fit_telegram_limit(self) -> None:
        job_id = "a" * 32
        session_id = "b" * 32
        payloads = [
            button.data
            for rows in (
                self.runtime._spoiler_buttons(job_id),
                self.runtime._collection_buttons(session_id),
                self.runtime._mode_buttons(),
            )
            for row in rows
            for button in row
        ]
        self.assertTrue(payloads)
        self.assertTrue(all(len(payload) <= 64 for payload in payloads))

    async def test_restart_reuses_existing_pending_confirmation_message(self) -> None:
        accepted = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=[incoming(110)],
            policy={"display_expected": True},
            spoiler_mode=SpoilerMode.ASK,
            ask_timeout_seconds=60,
        )
        await self.repo.save_job_display_message(
            JobDisplayMessage(
                job_id=accepted.job.id,
                chat_id=42,
                message_id=500,
            )
        )

        sent_before = len(self.client.sent)
        await self.runtime.recover(accepted.job)
        await self._drain()

        self.assertEqual(len(self.client.sent), sent_before)
        self.assertTrue(any(edit["message_id"] == 500 for edit in self.client.edits))
        self.assertEqual(self.runner.calls, [])

    async def test_missing_status_message_is_replaced_at_most_once_across_restarts(self) -> None:
        accepted = await self.intake.accept_once(
            owner_id=7,
            destination="@channel",
            media=[incoming(120)],
            policy={"display_expected": True},
            spoiler_mode=SpoilerMode.ALWAYS_NORMAL,
        )
        await self.repo.save_job_display_message(
            JobDisplayMessage(
                job_id=accepted.job.id,
                chat_id=42,
                message_id=500,
            )
        )
        self.client.fail_edit_ids.add(500)

        await self.runtime.recover(accepted.job)
        await self._drain()
        ref = await self.repo.get_job_display_message(accepted.job.id)
        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.replacement_count, 1)
        self.assertNotEqual(ref.message_id, 500)
        sends_after_replacement = len(self.client.sent)

        self.client.fail_edit_ids.add(ref.message_id)
        second_runner = RecordingRunner()
        second = TelethonIntakeRuntime(
            self.client,
            settings(),
            self.intake,
            second_runner,
        )
        try:
            await second.recover(accepted.job)
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.client.sent), sends_after_replacement)
            latest = await self.repo.get_job_display_message(accepted.job.id)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.replacement_count, 1)
            self.assertEqual(latest.message_id, ref.message_id)
        finally:
            await second.stop()


class IntakeRuntimeCollectionPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.database = Path(self.tmp.name) / "state.sqlite3"
        self.repo = SQLiteJobRepository(self.database)
        await self.repo.open()
        self.intake = IntakeService(self.repo)
        self.client = FakeClient()
        self.runner = RecordingRunner()
        self.runtime = TelethonIntakeRuntime(
            self.client,
            settings(collection_preview_enabled=True),
            self.intake,
            self.runner,
        )

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()
        await self.repo.close()
        self.tmp.cleanup()

    async def test_preview_requires_confirmation_before_enqueue(self) -> None:
        await self.runtime._begin_collection(42, 7)
        await self.runtime._on_message(
            FakeEvent(chat_id=42, sender_id=7, message=media_message(10))
        )
        await self.runtime._end_collection(42, 7)
        await asyncio.sleep(0.05)

        session = await self.intake.open_collection(owner_id=7, chat_id=42)
        self.assertIsNotNone(session)
        self.assertEqual(await self.repo.list_recent(owner_id=7, limit=10), [])
        preview_texts = [entry["text"] for entry in self.client.edits] + [
            entry["text"] for entry in self.client.sent
        ]
        self.assertTrue(any("合集发布预览" in text for text in preview_texts))
        assert session is not None

        await self.runtime._on_intake_callback(
            FakeEvent(
                chat_id=42,
                sender_id=7,
                data=b"intake:confirm:" + session.id.encode("utf-8"),
            )
        )
        await asyncio.sleep(0.05)
        self.assertIsNone(await self.intake.open_collection(owner_id=7, chat_id=42))
        jobs = await self.repo.list_recent(owner_id=7, limit=10)
        self.assertEqual(len(jobs), 1)

    async def test_home_preview_never_enqueues_when_preview_flag_disabled(self) -> None:
        self.runtime._settings.collection_preview_enabled = False
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        await self.runtime._on_message(FakeEvent(message=media_message(10)))
        event = FakeEvent(data=f"intake:preview:{session.id}".encode())
        await self.runtime._on_intake_callback(event)
        await self.runtime._on_intake_callback(event)
        self.assertEqual(await self.repo.list_recent(owner_id=7, limit=10), [])
        self.assertEqual(self.runner.calls, [])
        self.assertIsNotNone(await self.intake.open_collection(owner_id=7, chat_id=42))
        self.assertTrue(any("合集发布预览" in entry["text"] for entry in self.client.sent))

    async def test_all_navigation_labels_are_excluded_from_collection_caption(self) -> None:
        from tgvio.adapters.telegram.bot_ui_support import NAV_BUTTONS

        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        for label in NAV_BUTTONS:
            await self.runtime._on_message(FakeEvent(raw_text=label))
        self.assertEqual(await self.repo.count_collection_entries(session.id), (0, 0))
        await self.runtime._on_message(FakeEvent(message=media_message(10)))
        await self.runtime._on_message(FakeEvent(raw_text="真实文案"))
        self.assertEqual(await self.repo.count_collection_entries(session.id), (1, 1))

    async def test_abandon_cancels_without_creating_job(self) -> None:
        await self.runtime._begin_collection(42, 7)
        await self.runtime._on_message(
            FakeEvent(chat_id=42, sender_id=7, message=media_message(10))
        )
        await self.runtime._end_collection(42, 7)
        await asyncio.sleep(0.05)
        session = await self.intake.open_collection(owner_id=7, chat_id=42)
        assert session is not None

        await self.runtime._on_intake_callback(
            FakeEvent(
                chat_id=42,
                sender_id=7,
                data=b"intake:abandon:" + session.id.encode("utf-8"),
            )
        )
        await asyncio.sleep(0.05)
        self.assertIsNone(await self.intake.open_collection(owner_id=7, chat_id=42))
        self.assertEqual(await self.repo.list_recent(owner_id=7, limit=10), [])

    async def test_preview_projection_counts_and_cover_plan(self) -> None:
        session = await self.intake.begin_collection(owner_id=7, chat_id=42)
        await self.intake.add_collection_media(session, [incoming(1), incoming(2)])
        await self.intake.add_collection_text(
            session,
            text="a\n\nb",
            source_chat_id=42,
            source_message_id=3,
        )
        preview = await self.intake.preview_collection(
            owner_id=7,
            chat_id=42,
            cover_mode=True,
        )
        assert preview is not None
        self.assertEqual(preview.media_count, 2)
        self.assertEqual(preview.video_count, 2)
        self.assertEqual(preview.cover_plan, "首个视频截帧")
        self.assertEqual(preview.discussion_groups, 1)
        self.assertEqual(preview.caption_lines, 2)


class IntakeEditingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.intake = IntakeService(self.repo)
        self.tokens = OperationTokenService(self.repo)
        self.editing = CollectionEditingService(self.repo, self.intake, self.tokens)
        self.client = FakeClient()
        self.runner = RecordingRunner()
        self.runtime = TelethonIntakeRuntime(
            self.client,
            settings(),
            self.intake,
            self.runner,
            editing=self.editing,
        )

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()
        await self.repo.close()
        self.tmp.cleanup()

    async def _drain(self) -> None:
        await asyncio.sleep(0.05)

    async def test_end_uses_editing_preview_with_short_callbacks(self) -> None:
        await self.runtime._begin_collection(42, 7)
        await self.runtime._on_message(
            FakeEvent(chat_id=42, sender_id=7, message=media_message(10))
        )
        await self.runtime._end_collection(42, 7)
        await self._drain()
        self.assertEqual(await self.repo.list_recent(owner_id=7, limit=10), [])
        payloads = [
            button.data
            for message in self.client.edits + self.client.sent
            for row in (message.get("buttons") or [])
            for button in row
        ]
        self.assertTrue(payloads)
        self.assertTrue(all(len(payload) <= 64 for payload in payloads))
        self.assertTrue(any(payload.startswith(b"intake:ed:") for payload in payloads))

    async def test_caption_message_updates_draft_and_leaves_caption_out_of_media(self) -> None:
        await self.runtime._begin_collection(42, 7)
        session = await self.intake.open_collection(owner_id=7, chat_id=42)
        assert session is not None
        await self.runtime._on_message(
            FakeEvent(chat_id=42, sender_id=7, message=media_message(10))
        )
        draft = await self.editing.draft(session.id)
        assert draft is not None
        await self.editing.begin_caption(
            owner_id=7,
            chat_id=42,
            session_id=session.id,
            expected_revision=draft.revision,
        )
        await self.runtime._on_message(
            FakeEvent(chat_id=42, sender_id=7, raw_text="自定义封面文案")
        )
        draft = await self.editing.draft(session.id)
        assert draft is not None
        self.assertEqual(draft.caption_override, "自定义封面文案")
        counts = await self.intake.collection_counts(session.id)
        self.assertEqual(counts, (1, 0))

    async def test_confirm_token_creates_one_job_and_consumes_draft(self) -> None:
        await self.runtime._begin_collection(42, 7)
        session = await self.intake.open_collection(owner_id=7, chat_id=42)
        assert session is not None
        await self.runtime._on_message(
            FakeEvent(chat_id=42, sender_id=7, message=media_message(10))
        )
        draft = await self.editing.draft(session.id)
        assert draft is not None
        issued = await self.editing.issue_confirm(
            owner_id=7,
            session_id=session.id,
            expected_revision=draft.revision,
            spoiler_mode=SpoilerMode.SOURCE,
            style_policy=await self.runtime._resolve_style(7),
        )
        event = FakeEvent(chat_id=42, sender_id=7, data=f"intake:cc:{issued}".encode())
        await self.runtime._on_intake_callback(event)
        await self._drain()
        jobs = await self.repo.list_recent(owner_id=7, limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(await self.intake.open_collection(owner_id=7, chat_id=42))


if __name__ == "__main__":
    unittest.main()
