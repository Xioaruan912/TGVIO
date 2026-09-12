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
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
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

    async def list_recent(self, *, owner_id, limit):
        return [
            job
            for job in self.jobs.values()
            if job.owner_id == owner_id
        ][:limit]

    async def get(self, job_id):
        return self.jobs.get(job_id)

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

    async def retry_failed(self, job):
        self.retry_calls += 1
        job.state = JobState.RECEIVED
        job.error_code = None
        return RetryDecision(job=job, target_state=JobState.RECEIVED, retry_count=1)

    async def request_cancel(self, job, *, reason=None):
        self.cancel_calls += 1
        job.state = JobState.CANCELLED
        return job


class FakeArchiveOperator:
    def __init__(self) -> None:
        self.retry_calls = []

    async def retry_package(self, package_id):
        self.retry_calls.append(package_id)
        return SimpleNamespace(id=package_id)


class FakeCacheOperator:
    def __init__(self) -> None:
        self.cleanup_calls = 0

    async def cleanup(self, *, force=False):
        self.cleanup_calls += 1
        return SimpleNamespace(removed_jobs=2, removed_bytes=14, blocked_by_archive=1)

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
        items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture", size_bytes=7)],
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

    def test_command_surface_contains_only_new_tgvio_commands(self) -> None:
        names = {name for name, _ in COMMANDS}
        self.assertEqual(names, {"start", "jobs", "status", "help"})
        self.assertFalse(
            names & {"queue", "begin", "end", "profiles", "webdav", "backup", "dashboard"}
        )

    def test_mobile_reply_keyboard_is_persistent_and_compact(self) -> None:
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository())
        keyboard = ui._reply_keyboard()
        labels = {
            button.button.text
            for row in keyboard
            for button in row
        }
        self.assertEqual(labels, set(NAV_BUTTONS))
        self.assertEqual([len(row) for row in keyboard], [2, 2, 2])
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
            "archive-retry",
            "archive-retry-confirm",
        ):
            payload = ui._callback_data(action, "f" * 32)
            self.assertLessEqual(len(payload), 64)

    async def test_jobs_page_uses_direct_buttons_and_friendly_failure_text(self) -> None:
        failed = job()
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository([failed]))
        text, buttons = await ui._jobs_page(42)

        self.assertIn("暂时无法读取原媒体", text)
        self.assertNotIn("download_failed", text)
        self.assertNotIn("/job", text)
        payloads = [
            button.data
            for row in buttons
            for button in row
            if getattr(button, "data", None)
        ]
        self.assertIn(f"ui:job:{failed.id}".encode(), payloads)

    async def test_normal_job_detail_hides_internal_code_but_deep_view_keeps_it(self) -> None:
        failed = job()
        ui = TelethonBotUI(FakeClient(), settings(), FakeRepository([failed]))

        normal = await ui._job_text(42, failed.id)
        deep = await ui._job_text(42, failed.id, deep=True)

        self.assertIn("暂时无法读取原媒体", normal)
        self.assertNotIn("`download_failed`", normal)
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
