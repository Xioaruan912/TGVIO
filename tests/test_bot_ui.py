from __future__ import annotations

from types import SimpleNamespace
import unittest

from telethon import events
from telethon.tl import functions, types

from tgvio.adapters.telegram.bot_ui import (
    COMMANDS,
    NAV_BUTTONS,
    TelethonBotUI,
)
from tgvio.application.job_control import RetryDecision
from tgvio.application.operation_tokens import OperationTokenInvalidError
from tgvio.application.undo import UndoOperationInvalidError
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.control import JobControlState, QueueControlState
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.job_query import JobListFilter
from tgvio.domain.publish import PublishPlan, PublishStep, PublishStepKind, PublishTarget


class FakeClient:
    def __init__(self) -> None:
        self.requests = []
        self.handlers = []

    async def __call__(self, request):
        self.requests.append(request)
        return True

    def add_event_handler(self, callback, event) -> None:
        self.handlers.append((callback, event))


class FakeRepository:
    def __init__(self, jobs=(), *, archives=(), plans=()) -> None:
        self.jobs = {job.id: job for job in jobs}
        self.archives = {package.job_id: package for package in archives}
        self.plans = {plan.job_id: plan for plan in plans}
        self.controls = {job.id: JobControlState(job_id=job.id) for job in jobs}
        self.accepted_orders = {job.id: index for index, job in enumerate(jobs, start=1)}
        self.queue_control = QueueControlState()

    async def list_recent(self, *, owner_id, limit):
        return [
            job
            for job in self.jobs.values()
            if job.owner_id == owner_id
        ][:limit]

    async def list_by_states(self, states):
        return [job for job in self.jobs.values() if job.state in states]

    async def get(self, job_id):
        return self.jobs.get(job_id)

    async def get_accepted_order(self, job_id):
        return self.accepted_orders.get(job_id)

    async def get_by_accepted_order(self, owner_id, accepted_order):
        for job_id, order in self.accepted_orders.items():
            job = self.jobs.get(job_id)
            if order == accepted_order and job is not None and job.owner_id == owner_id:
                return job
        return None

    async def count_by_state(self, *, owner_id=None):
        counts = {}
        for value in self.jobs.values():
            if owner_id is not None and value.owner_id != owner_id:
                continue
            counts[value.state] = counts.get(value.state, 0) + 1
        return counts

    async def get_runtime_health(self):
        return {"telegram": {"status": "connected"}, "runtime": {"status": "alive"}}

    async def get_job_progress(self, job_id):
        return None

    async def get_archive_package_for_job(self, job_id):
        return self.archives.get(job_id)

    async def list_recent_archive_packages(self, *, owner_id, limit):
        owned = {job.id for job in self.jobs.values() if job.owner_id == owner_id}
        return [
            package for package in self.archives.values() if package.job_id in owned
        ][:limit]

    async def get_publish_plan(self, job_id):
        return self.plans.get(job_id)

    async def list_events(self, job_id):
        return []

    async def list_publish_effects(self, plan_id):
        return []

    async def list_archive_events(self, package_id):
        return []

    async def get_job_control(self, job_id):
        return self.controls.setdefault(job_id, JobControlState(job_id=job_id))

    async def get_job_display_message(self, job_id):
        return None

    async def get_queue_control(self):
        return self.queue_control


class FakeEvent:
    def __init__(self, *, sender_id=42, data=b"", raw_text="", chat_id=42) -> None:
        self.sender_id = sender_id
        self.data = data
        self.raw_text = raw_text
        self.chat_id = chat_id
        self.edits = []
        self.answers = []
        self.responses = []
        self.edit_exception = None

    async def edit(self, text, **kwargs):
        if self.edit_exception is not None:
            raise self.edit_exception
        self.edits.append((text, kwargs))

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def respond(self, text, **kwargs):
        self.responses.append((text, kwargs))


class MessageNotModifiedError(Exception):
    pass


class FakeControl:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository
        self.retry_calls = 0
        self.cancel_calls = 0
        self.hold_calls = 0
        self.resume_calls = 0
        self.pause_queue_calls = 0
        self.resume_queue_calls = 0

    async def retry_failed(self, job):
        self.retry_calls += 1
        job.state = JobState.RECEIVED
        job.error_code = None
        return RetryDecision(job=job, target_state=JobState.RECEIVED, retry_count=1)

    async def request_cancel(self, job, *, reason=None):
        self.cancel_calls += 1
        job.state = JobState.CANCELLED
        return job

    async def request_hold(self, job, *, reason=None):
        self.hold_calls += 1
        current = await self.repository.get_job_control(job.id)
        updated = JobControlState(
            job_id=job.id,
            hold_requested=True,
            hold_reason=reason,
            hold_revision=current.hold_revision + 1,
        )
        self.repository.controls[job.id] = updated
        return updated

    async def resume(self, job):
        self.resume_calls += 1
        current = await self.repository.get_job_control(job.id)
        updated = JobControlState(
            job_id=job.id,
            hold_requested=False,
            hold_revision=current.hold_revision + 1,
        )
        self.repository.controls[job.id] = updated
        return updated

    async def pause_queue(self, *, reason=None):
        self.pause_queue_calls += 1
        current = self.repository.queue_control
        self.repository.queue_control = QueueControlState(
            paused=True,
            pause_reason=reason,
            revision=current.revision + 1,
        )
        return self.repository.queue_control

    async def resume_queue(self):
        self.resume_queue_calls += 1
        current = self.repository.queue_control
        self.repository.queue_control = QueueControlState(
            paused=False,
            revision=current.revision + 1,
        )
        return self.repository.queue_control


class FakeArchiveOperator:
    def __init__(self) -> None:
        self.retry_calls = []

    async def retry_package(self, package_id):
        self.retry_calls.append(package_id)
        return SimpleNamespace(id=package_id)


class FakeOperationTokens:
    def __init__(self) -> None:
        self.operations = {}
        self.consumed: set[str] = set()
        self.consume_calls = 0

    async def issue(
        self,
        *,
        owner_id,
        action,
        resource_type,
        resource_id,
        expected_revision,
        payload,
    ):
        token = f"op-{action}"
        operation = SimpleNamespace(
            token=token,
            owner_id=owner_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            expected_revision=expected_revision,
            payload=payload,
            consumed_at=None,
        )
        self.operations[token] = operation
        return operation

    async def inspect(self, *, token, owner_id, action):
        operation = self.operations.get(token)
        if (
            operation is None
            or operation.owner_id != owner_id
            or operation.action != action
            or token in self.consumed
        ):
            raise OperationTokenInvalidError("invalid")
        return operation

    async def consume(
        self,
        *,
        token,
        owner_id,
        action,
        resource_type,
        resource_id,
        expected_revision,
        payload,
    ):
        operation = await self.inspect(token=token, owner_id=owner_id, action=action)
        if (
            operation.resource_type != resource_type
            or operation.resource_id != resource_id
            or operation.expected_revision != expected_revision
            or operation.payload != payload
        ):
            raise OperationTokenInvalidError("stale")
        self.consume_calls += 1
        self.consumed.add(token)
        return operation


class FakeUndoService:
    def __init__(
        self,
        *,
        partial: bool = False,
        invalid: bool = False,
        error: bool = False,
        status_error: bool = False,
    ) -> None:
        self.prepare_calls = 0
        self.confirm_calls = 0
        self.partial = partial
        self.invalid = invalid
        self.error = error
        self.status_error = status_error
        self.completed = False

    async def status(self, job):
        if self.status_error:
            raise RuntimeError("fixture status unavailable")
        return SimpleNamespace(remaining_messages=0 if self.completed else 2)

    async def prepare(self, job, *, owner_id):
        self.prepare_calls += 1
        status = SimpleNamespace(
            remaining_messages=2,
            channel_messages=1,
            discussion_messages=1,
            remaining_channel_messages=1,
            remaining_discussion_messages=1,
        )
        return SimpleNamespace(
            operation=SimpleNamespace(token="undo-token-1234"),
            status=status,
        )

    async def confirm(self, *, owner_id, token):
        self.confirm_calls += 1
        if self.invalid:
            raise UndoOperationInvalidError("expired")
        if self.error:
            raise RuntimeError("fixture checkpoint unavailable")
        if self.partial:
            return SimpleNamespace(
                job_id="a" * 32,
                complete=False,
                deleted_now=1,
                deleted_total=1,
                total_messages=2,
                remaining_messages=1,
            )
        self.completed = True
        return SimpleNamespace(
            job_id="a" * 32,
            complete=True,
            deleted_now=2,
            deleted_total=2,
            total_messages=2,
            remaining_messages=0,
        )


class FakeCacheOperator:
    def __init__(self) -> None:
        self.cleanup_calls = 0
        self.cleanup_job_ids = None
        self.candidate_ids = ("job-a", "job-b")

    async def cleanup(self, *, force=False, job_ids=None):
        self.cleanup_calls += 1
        self.cleanup_job_ids = job_ids
        return SimpleNamespace(removed_jobs=2, removed_bytes=14, blocked_by_archive=1)

    async def cleanup_candidates(self, *, force=False):
        return self.candidate_ids

    async def stats(self):
        return SimpleNamespace(
            bytes_used=14,
            managed_dirs=2,
            retention_hours=24,
            eligible_jobs=2,
            blocked_by_archive=1,
        )


def settings(**overrides):
    values = {
        "allowed_users": (42,),
        "publish_enabled": True,
        "archive_enabled": True,
        "url_enabled": True,
        "live_fixture_enabled": False,
        "download_dir": "/tmp",
        "environment": "test",
        "worker_concurrency": 2,
        "url_private_network_policy": "block",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def job(*, owner_id=42, state=JobState.FAILED, error_code="download_failed"):
    return Job(
        id="a" * 32,
        owner_id=owner_id,
        destination="@channel",
        state=state,
        error_code=error_code,
        created_at="2026-09-12 23:42:00",
        items=[
            MediaItem(
                index=0,
                kind=MediaKind.VIDEO,
                source="fixture",
                name="旅行_01.mp4",
                size_bytes=7,
            )
        ],
    )


class BotUIConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_menu_resets_legacy_commands_and_sets_tgvio_commands(self) -> None:
        client = FakeClient()
        ui = TelethonBotUI(client, SimpleNamespace(allowed_users=(42,)), SimpleNamespace())
        await ui.configure_server_menu()

        resets = [request for request in client.requests if isinstance(request, functions.bots.ResetBotCommandsRequest)]
        sets = [request for request in client.requests if isinstance(request, functions.bots.SetBotCommandsRequest)]
        menus = [request for request in client.requests if isinstance(request, functions.bots.SetBotMenuButtonRequest)]
        self.assertEqual({request.lang_code for request in resets}, {"", "zh", "en"})
        self.assertEqual(len(resets), 12)
        self.assertEqual(
            {type(request.scope).__name__ for request in resets},
            {
                "BotCommandScopeDefault",
                "BotCommandScopeUsers",
                "BotCommandScopeChats",
                "BotCommandScopeChatAdmins",
            },
        )
        self.assertEqual({request.lang_code for request in sets}, {"", "zh", "en"})
        for request in sets:
            self.assertEqual(
                [(command.command, command.description) for command in request.commands],
                list(COMMANDS),
            )
        self.assertEqual(len(menus), 1)
        self.assertIsInstance(menus[0].button, types.BotMenuButtonCommands)

    def test_command_surface_contains_collection_and_spoiler_controls(self) -> None:
        names = {name for name, _ in COMMANDS}
        self.assertEqual(
            names,
            {"start", "begin", "end", "mode", "pause", "resume", "jobs", "status", "help"},
        )
        self.assertFalse(
            names & {"queue", "profiles", "webdav", "backup", "dashboard"}
        )

    def test_mobile_reply_keyboard_is_persistent_and_compact(self) -> None:
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository())
        keyboard = ui._reply_keyboard()
        labels = {
            button.button.text
            for row in keyboard
            for button in row
        }
        self.assertEqual(
            labels,
            set(NAV_BUTTONS) | {"📥 开始合集", "🛑 结束并发布"},
        )
        self.assertEqual([len(row) for row in keyboard], [2, 2, 2, 2])
        self.assertTrue(all(button.persistent for row in keyboard for button in row))
        self.assertTrue(all(button.resize for row in keyboard for button in row))

    async def test_mobile_navigation_text_is_consumed_before_media_intake(self) -> None:
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository())
        event = FakeEvent(raw_text="🏠 首页")

        with self.assertRaises(events.StopPropagation):
            await ui._on_nav_button(event)

        self.assertEqual(len(event.responses), 1)
        self.assertIn("常驻按钮", event.responses[0][0])

    def test_all_job_callback_payloads_fit_telegram_limit(self) -> None:
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository())
        for action in (
            "job",
            "job-deep",
            "plan",
            "retry",
            "retry-confirm",
            "cancel",
            "cancel-confirm",
            "hold",
            "resume",
            "archive-retry",
            "archive-retry-confirm",
            "undo",
            "undo-confirm",
        ):
            payload = ui._callback_data(action, "f" * 32)
            self.assertLessEqual(len(payload), 64)

    async def test_undo_requires_confirmation_before_service_confirm(self) -> None:
        completed = job(state=JobState.SUCCEEDED, error_code=None)
        repository = FakeRepository([completed])
        undo = FakeUndoService()
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            undo_service=undo,  # type: ignore[arg-type]
        )

        buttons = await ui._job_buttons(completed)
        payloads = [
            button.data
            for row in buttons
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(f"ui:undo:{completed.id}".encode(), payloads)

        request = FakeEvent(data=f"ui:undo:{completed.id}".encode())
        await ui._on_callback(request)
        self.assertEqual(undo.prepare_calls, 1)
        self.assertEqual(undo.confirm_calls, 0)
        self.assertIn("确认撤销发布", request.edits[0][0])
        confirm_payloads = [
            button.data
            for row in request.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(b"ui:undo-confirm:undo-token-1234", confirm_payloads)

        confirm = FakeEvent(data=b"ui:undo-confirm:undo-token-1234")
        await ui._on_callback(confirm)
        self.assertEqual(undo.confirm_calls, 1)
        self.assertIn("撤销完成", confirm.edits[0][0])
        self.assertTrue(confirm.answers)

    async def test_partial_undo_offers_continue_without_reusing_successful_items(self) -> None:
        completed = job(state=JobState.SUCCEEDED, error_code=None)
        repository = FakeRepository([completed])
        undo = FakeUndoService(partial=True)
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            undo_service=undo,  # type: ignore[arg-type]
        )
        event = FakeEvent(data=b"ui:undo-confirm:undo-token-1234")

        await ui._on_callback(event)

        self.assertIn("撤销未完全完成", event.edits[0][0])
        payloads = [
            button.data
            for row in event.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(f"ui:undo:{completed.id}".encode(), payloads)

    async def test_expired_undo_token_does_not_call_delete_path_twice(self) -> None:
        completed = job(state=JobState.SUCCEEDED, error_code=None)
        undo = FakeUndoService(invalid=True)
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            FakeRepository([completed]),
            undo_service=undo,  # type: ignore[arg-type]
        )
        event = FakeEvent(data=b"ui:undo-confirm:expired-token")

        await ui._on_callback(event)

        self.assertEqual(undo.confirm_calls, 1)
        self.assertIn("撤销操作已过期", event.edits[0][0])

    async def test_undo_infrastructure_error_keeps_task_ui_usable(self) -> None:
        completed = job(state=JobState.SUCCEEDED, error_code=None)
        undo = FakeUndoService(error=True, status_error=True)
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            FakeRepository([completed]),
            undo_service=undo,  # type: ignore[arg-type]
        )

        text = await ui._job_text(42, completed.id)
        buttons = await ui._job_buttons(completed)
        self.assertIn("任务详情", text)
        self.assertNotIn(
            f"ui:undo:{completed.id}".encode(),
            [button.data for row in buttons for button in row if getattr(button, "data", None)],
        )

        event = FakeEvent(data=b"ui:undo-confirm:broken-token")
        await ui._on_callback(event)
        self.assertIn("撤销暂未完成", event.edits[0][0])

    async def test_jobs_page_uses_direct_buttons_and_friendly_failure_text(self) -> None:
        failed = job()
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository([failed]))
        text, buttons = await ui._jobs_page(42)

        self.assertIn("暂时无法读取原媒体", text)
        self.assertIn("任务 #1", text)
        self.assertIn("09-13 07:42", text)
        self.assertIn("旅行\\_01.mp4", text)
        self.assertNotIn(failed.id[:10], text)
        self.assertNotIn("download_failed", text)
        self.assertNotIn("/job", text)
        payloads = [
            button.data
            for row in buttons
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(f"ui:job:{failed.id}".encode(), payloads)
        self.assertIn(b"ui:failures:0", payloads)
        self.assertTrue(all(len(payload) <= 64 for payload in payloads))

    async def test_jobs_filter_callback_renders_only_selected_state(self) -> None:
        active = Job(
            id="a" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.DOWNLOADING,
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="active")],
        )
        completed = Job(
            id="b" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            items=[MediaItem(index=0, kind=MediaKind.PHOTO, source="completed")],
        )
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository([active, completed]))
        event = FakeEvent(data=b"ui:jobs:completed:0")

        await ui._on_callback(event)

        text = event.edits[0][0]
        self.assertIn("完成", text)
        self.assertIn("任务 #2", text)
        self.assertNotIn(completed.id[:10], text)
        self.assertNotIn(active.id[:10], text)
        payloads = [
            button.data
            for row in event.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertTrue(all(len(payload) <= 64 for payload in payloads))

    async def test_failure_center_hides_auto_recovery_pending_failure(self) -> None:
        failed = Job(
            id="c" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.FAILED,
            error_code="download_failed",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="failed")],
        )
        pending = Job(
            id="d" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.FAILED,
            error_code="download_failed",
            policy={
                "auto_recovery": {
                    "version": 1,
                    "enabled": True,
                    "max_attempts": 3,
                    "base_delay_seconds": 15,
                    "max_delay_seconds": 300,
                }
            },
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="pending")],
        )
        repository = FakeRepository([failed, pending])
        ui = TelethonBotUI(FakeClient(), settings(), repository)
        event = FakeEvent(data=b"ui:failures:0")

        await ui._on_callback(event)

        text = event.edits[0][0]
        self.assertIn("任务 #1", text)
        self.assertNotIn("任务 #2", text)
        self.assertNotIn(failed.id[:10], text)
        self.assertNotIn(pending.id[:10], text)
        self.assertIn("需要处理", text)

    async def test_task_number_resolves_without_uuid_memory(self) -> None:
        first = job(state=JobState.SUCCEEDED, error_code=None)
        second = Job(
            id="b" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.FAILED,
            error_code="download_failed",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository([first, second]))

        by_hash_number = await ui._resolve_job(42, "#2")
        by_plain_number = await ui._resolve_job(42, "2")

        self.assertIsNotNone(by_hash_number)
        self.assertEqual(by_hash_number.id, second.id)
        self.assertEqual(by_plain_number.id, second.id)

    async def test_missing_numeric_task_number_never_matches_uuid_prefix(self) -> None:
        numeric_prefix = Job(
            id="24" + "a" * 30,
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        numeric_exact = Job(
            id="1" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            FakeRepository([numeric_prefix, numeric_exact]),
        )

        self.assertIsNone(await ui._resolve_job(42, "24"))
        self.assertIsNone(await ui._resolve_job(42, "#24"))
        self.assertEqual((await ui._resolve_job(42, numeric_exact.id)).id, numeric_exact.id)

    async def test_terminal_job_with_stale_hold_flag_is_not_rendered_as_held(self) -> None:
        completed = job(state=JobState.SUCCEEDED, error_code=None)
        repository = FakeRepository([completed])
        repository.controls[completed.id] = JobControlState(
            job_id=completed.id,
            hold_requested=True,
            hold_reason="stale",
            hold_revision=1,
        )
        ui = TelethonBotUI(FakeClient(), settings(), repository)

        all_text, _ = await ui._jobs_page(42)
        held_text, _ = await ui._jobs_page(42, filter=JobListFilter.HELD)

        self.assertIn("已完成", all_text)
        self.assertNotIn("已暂停", all_text)
        self.assertIn("共 `0` 个任务", held_text)

    async def test_normal_job_detail_hides_internal_code_but_deep_view_keeps_it(self) -> None:
        failed = job()
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository([failed]))

        normal = await ui._job_text(42, failed.id)
        deep = await ui._job_text(42, failed.id, deep=True)

        self.assertIn("任务 #1", normal)
        self.assertIn("旅行\\_01.mp4", normal)
        self.assertIn("暂时无法读取原媒体", normal)
        self.assertNotIn(failed.id, normal)
        self.assertNotIn("`download_failed`", normal)
        self.assertIn(f"内部 Job ID：`{failed.id}`", deep)
        self.assertIn("内部错误码：`download_failed`", deep)

    async def test_uncertain_publish_never_gets_retry_button(self) -> None:
        uncertain = job(error_code="publish_uncertain")
        repository = FakeRepository([uncertain])
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            control=FakeControl(repository),
        )

        buttons = await ui._job_buttons(uncertain)
        payloads = [
            button.data
            for row in buttons
            for button in row
            if getattr(button, "data", None)
        ]

        self.assertNotIn(f"ui:retry:{uncertain.id}".encode(), payloads)

    async def test_repeat_page_click_is_acknowledged_without_unhandled_error(self) -> None:
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository())
        event = FakeEvent(data=b"ui:home")
        event.edit_exception = MessageNotModifiedError("unchanged")

        await ui._on_callback(event)

        self.assertEqual(event.answers[0][0], "已经是最新页面")

    async def test_job_callback_enforces_owner_from_durable_job(self) -> None:
        foreign = job(owner_id=7)
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository([foreign]))
        event = FakeEvent(data=f"ui:job:{foreign.id}".encode())

        await ui._on_callback(event)

        self.assertFalse(event.edits)
        self.assertEqual(event.answers[0][0], "任务不存在或无权限")
        self.assertTrue(event.answers[0][1]["alert"])

    async def test_archive_retry_callback_cannot_cross_job_owner(self) -> None:
        foreign = job(owner_id=7, state=JobState.SUCCEEDED, error_code=None)
        package = ArchivePackage(
            id=f"arc_{foreign.id}",
            job_id=foreign.id,
            layout_version="v1",
            remote_path="archive/x",
            staging_path=".staging/x",
            state=ArchivePackageState.FAILED,
            manifest={},
            objects=(),
        )
        archive = FakeArchiveOperator()
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            FakeRepository([foreign], archives=[package]),
            archive_operator=archive,
        )
        event = FakeEvent(data=f"ui:archive-retry-confirm:{foreign.id}".encode())

        await ui._on_callback(event)

        self.assertFalse(archive.retry_calls)
        self.assertEqual(event.answers[0][0], "任务不存在或无权限")

    async def test_retry_button_requires_confirmation_then_schedules(self) -> None:
        failed = job()
        repository = FakeRepository([failed])
        control = FakeControl(repository)
        scheduled = []
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            control=control,
            schedule_job=lambda value, **kwargs: scheduled.append((value, kwargs)),
        )
        request = FakeEvent(data=f"ui:retry:{failed.id}".encode())

        await ui._on_callback(request)

        self.assertEqual(control.retry_calls, 0)
        confirmation_payloads = [
            button.data
            for row in request.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(f"ui:retry-confirm:{failed.id}".encode(), confirmation_payloads)

        confirm = FakeEvent(data=f"ui:retry-confirm:{failed.id}".encode())
        await ui._on_callback(confirm)

        self.assertEqual(control.retry_calls, 1)
        self.assertEqual(len(scheduled), 1)
        self.assertIn("安全重试", confirm.edits[0][0])

    async def test_cancel_button_requires_confirmation(self) -> None:
        active = job(state=JobState.DOWNLOADING, error_code=None)
        repository = FakeRepository([active])
        control = FakeControl(repository)
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            control=control,
        )
        request = FakeEvent(data=f"ui:cancel:{active.id}".encode())

        await ui._on_callback(request)

        self.assertEqual(control.cancel_calls, 0)
        self.assertIn("确认取消任务", request.edits[0][0])

        confirm = FakeEvent(data=f"ui:cancel-confirm:{active.id}".encode())
        await ui._on_callback(confirm)

        self.assertEqual(control.cancel_calls, 1)
        self.assertIn("已取消", confirm.edits[0][0])

    async def test_cancel_confirmation_uses_single_use_operation_token_when_enabled(self) -> None:
        active = job(state=JobState.DOWNLOADING, error_code=None)
        repository = FakeRepository([active])
        control = FakeControl(repository)
        operations = FakeOperationTokens()
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            control=control,
            operation_tokens=operations,  # type: ignore[arg-type]
        )
        request = FakeEvent(data=f"ui:cancel:{active.id}".encode())

        await ui._on_callback(request)

        payloads = [
            button.data
            for row in request.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(b"ui:cancel-confirm:op-cancel_job", payloads)
        self.assertEqual(control.cancel_calls, 0)

        confirm = FakeEvent(data=b"ui:cancel-confirm:op-cancel_job")
        await ui._on_callback(confirm)
        self.assertEqual(control.cancel_calls, 1)
        self.assertEqual(operations.consume_calls, 1)

        repeated = FakeEvent(data=b"ui:cancel-confirm:op-cancel_job")
        await ui._on_callback(repeated)
        self.assertEqual(control.cancel_calls, 1)
        self.assertEqual(operations.consume_calls, 1)
        self.assertIn("确认操作已过期", repeated.answers[0][0])

    async def test_retry_confirmation_uses_operation_token_when_enabled(self) -> None:
        failed = job()
        repository = FakeRepository([failed])
        control = FakeControl(repository)
        operations = FakeOperationTokens()
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            control=control,
            operation_tokens=operations,  # type: ignore[arg-type]
        )
        request = FakeEvent(data=f"ui:retry:{failed.id}".encode())

        await ui._on_callback(request)
        payloads = [
            button.data
            for row in request.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(b"ui:retry-confirm:op-retry_job", payloads)
        self.assertEqual(control.retry_calls, 0)

        confirm = FakeEvent(data=b"ui:retry-confirm:op-retry_job")
        await ui._on_callback(confirm)
        self.assertEqual(operations.consume_calls, 1)
        self.assertEqual(control.retry_calls, 1)

    async def test_hold_and_resume_buttons_toggle_durable_control_and_reschedule(self) -> None:
        active = job(state=JobState.DOWNLOADING, error_code=None)
        repository = FakeRepository([active])
        control = FakeControl(repository)
        scheduled = []
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            control=control,
            schedule_job=lambda value, **kwargs: scheduled.append((value, kwargs)),
        )

        hold = FakeEvent(data=f"ui:hold:{active.id}".encode())
        await ui._on_callback(hold)
        self.assertEqual(control.hold_calls, 1)
        self.assertEqual(active.state, JobState.DOWNLOADING)
        state = await repository.get_job_control(active.id)
        self.assertTrue(state.hold_requested)
        hold_payloads = [
            button.data
            for row in hold.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(f"ui:resume:{active.id}".encode(), hold_payloads)

        resume = FakeEvent(data=f"ui:resume:{active.id}".encode())
        await ui._on_callback(resume)
        self.assertEqual(control.resume_calls, 1)
        self.assertFalse((await repository.get_job_control(active.id)).hold_requested)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0].id, active.id)
        self.assertIn("已恢复", resume.edits[0][0])

    async def test_queue_pause_requires_confirmation_and_resume_reschedules_jobs(self) -> None:
        active = job(state=JobState.DOWNLOADING, error_code=None)
        repository = FakeRepository([active])
        control = FakeControl(repository)
        scheduled = []
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            control=control,
            schedule_job=lambda value, **kwargs: scheduled.append((value, kwargs)),
        )

        request = FakeEvent(data=b"ui:queue-pause")
        await ui._on_callback(request)
        self.assertEqual(control.pause_queue_calls, 0)
        confirmation_payloads = [
            button.data
            for row in request.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(b"ui:queue-pause-confirm", confirmation_payloads)

        confirm = FakeEvent(data=b"ui:queue-pause-confirm")
        await ui._on_callback(confirm)
        self.assertEqual(control.pause_queue_calls, 1)
        self.assertTrue(repository.queue_control.paused)
        self.assertIn("已暂停", confirm.edits[0][0])

        resume = FakeEvent(data=b"ui:queue-resume")
        await ui._on_callback(resume)
        self.assertEqual(control.resume_queue_calls, 1)
        self.assertFalse(repository.queue_control.paused)
        self.assertEqual([value.id for value, _kwargs in scheduled], [active.id])
        self.assertIn("运行中", resume.edits[0][0])

    async def test_archive_retry_button_requires_confirmation(self) -> None:
        completed = job(state=JobState.SUCCEEDED, error_code=None)
        package = ArchivePackage(
            id=f"arc_{completed.id}",
            job_id=completed.id,
            layout_version="v1",
            remote_path="archive/x",
            staging_path=".staging/x",
            state=ArchivePackageState.FAILED,
            manifest={},
            objects=(),
            error_code="archive_execution_failed",
        )
        repository = FakeRepository([completed], archives=[package])
        archive = FakeArchiveOperator()
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            archive_operator=archive,
        )
        request = FakeEvent(data=f"ui:archive-retry:{completed.id}".encode())

        await ui._on_callback(request)

        self.assertFalse(archive.retry_calls)
        self.assertIn("确认重传归档", request.edits[0][0])

        confirm = FakeEvent(
            data=f"ui:archive-retry-confirm:{completed.id}".encode()
        )
        await ui._on_callback(confirm)

        self.assertEqual(archive.retry_calls, [package.id])
        self.assertIn("重新排队", confirm.edits[0][0])

    async def test_archive_retry_confirmation_uses_operation_token_when_enabled(self) -> None:
        completed = job(state=JobState.SUCCEEDED, error_code=None)
        package = ArchivePackage(
            id=f"arc_{completed.id}",
            job_id=completed.id,
            layout_version="v1",
            remote_path="archive/x",
            staging_path=".staging/x",
            state=ArchivePackageState.FAILED,
            manifest={},
            objects=(),
            error_code="archive_execution_failed",
        )
        repository = FakeRepository([completed], archives=[package])
        archive = FakeArchiveOperator()
        operations = FakeOperationTokens()
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            repository,
            archive_operator=archive,
            operation_tokens=operations,  # type: ignore[arg-type]
        )

        request = FakeEvent(data=f"ui:archive-retry:{completed.id}".encode())
        await ui._on_callback(request)
        payloads = [
            button.data
            for row in request.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(b"ui:archive-retry-confirm:op-archive_retry", payloads)
        self.assertFalse(archive.retry_calls)

        confirm = FakeEvent(data=b"ui:archive-retry-confirm:op-archive_retry")
        await ui._on_callback(confirm)
        self.assertEqual(operations.consume_calls, 1)
        self.assertEqual(archive.retry_calls, [package.id])

    async def test_cache_cleanup_button_requires_confirmation(self) -> None:
        cache = FakeCacheOperator()
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            FakeRepository(),
            cache_operator=cache,
        )
        request = FakeEvent(data=b"ui:cache-clean")

        await ui._on_callback(request)

        self.assertEqual(cache.cleanup_calls, 0)
        self.assertIn("确认清理缓存", request.edits[0][0])

        confirm = FakeEvent(data=b"ui:cache-clean-confirm")
        await ui._on_callback(confirm)

        self.assertEqual(cache.cleanup_calls, 1)
        self.assertIn("已清理", confirm.edits[0][0])

    async def test_cache_cleanup_token_binds_exact_candidate_set(self) -> None:
        cache = FakeCacheOperator()
        operations = FakeOperationTokens()
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            FakeRepository(),
            cache_operator=cache,
            operation_tokens=operations,  # type: ignore[arg-type]
        )
        request = FakeEvent(data=b"ui:cache-clean")

        await ui._on_callback(request)

        self.assertIn("精确匹配：`2`", request.edits[0][0])
        payloads = [
            button.data
            for row in request.edits[0][1]["buttons"]
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(b"ui:cache-clean-confirm:op-cache_cleanup", payloads)

        confirm = FakeEvent(data=b"ui:cache-clean-confirm:op-cache_cleanup")
        await ui._on_callback(confirm)

        self.assertEqual(operations.consume_calls, 1)
        self.assertEqual(cache.cleanup_job_ids, cache.candidate_ids)

    async def test_cache_cleanup_token_fails_closed_when_candidates_change(self) -> None:
        cache = FakeCacheOperator()
        operations = FakeOperationTokens()
        ui = TelethonBotUI(
            FakeClient(),
            settings(),
            FakeRepository(),
            cache_operator=cache,
            operation_tokens=operations,  # type: ignore[arg-type]
        )
        await ui._on_callback(FakeEvent(data=b"ui:cache-clean"))
        cache.candidate_ids = ("job-b",)

        confirm = FakeEvent(data=b"ui:cache-clean-confirm:op-cache_cleanup")
        await ui._on_callback(confirm)

        self.assertEqual(operations.consume_calls, 0)
        self.assertEqual(cache.cleanup_calls, 0)
        self.assertIn("确认操作已过期", confirm.answers[0][0])

    def test_live_fixture_gate_accepts_only_small_planned_jobs(self) -> None:
        settings = SimpleNamespace(
            allowed_users=(42,),
            live_fixture_max_bytes=100 * 1024 * 1024,
        )
        ui = TelethonBotUI(FakeClient(), settings, SimpleNamespace())
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.PHOTO,
                    source="fixture",
                    size_bytes=1024,
                )
            ],
        )
        plan = PublishPlan(
            job_id=job.id,
            summary={},
            steps=(
                PublishStep(
                    index=0,
                    kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
                    target=PublishTarget.CHANNEL,
                    item_indexes=(0,),
                    params={"strategies": {"0": "native"}},
                ),
            ),
        )
        self.assertIsNone(ui._fixture_validation_error(job, plan))

        job.state = JobState.ANALYZED
        self.assertIn("PLANNED", ui._fixture_validation_error(job, plan) or "")

    def test_live_fixture_gate_rejects_large_file_strategies(self) -> None:
        settings = SimpleNamespace(
            allowed_users=(42,),
            live_fixture_max_bytes=100 * 1024 * 1024,
        )
        ui = TelethonBotUI(FakeClient(), settings, SimpleNamespace())
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[
                MediaItem(
                    index=0,
                    kind=MediaKind.VIDEO,
                    source="fixture",
                    size_bytes=1024,
                )
            ],
        )
        plan = PublishPlan(
            job_id=job.id,
            summary={},
            steps=(
                PublishStep(
                    index=0,
                    kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
                    target=PublishTarget.CHANNEL,
                    item_indexes=(0,),
                    params={"strategies": {"0": "split_playable"}},
                ),
            ),
        )
        self.assertIn("分段/分卷", ui._fixture_validation_error(job, plan) or "")
