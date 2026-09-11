from __future__ import annotations

from types import SimpleNamespace
import unittest

from telethon.tl import functions, types

from tgvio.adapters.telegram.bot_ui import COMMANDS, TelethonBotUI
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.publish import PublishPlan, PublishStep, PublishStepKind, PublishTarget


class FakeClient:
    def __init__(self) -> None:
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return True


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
        self.assertEqual(
            names,
            {
                "start",
                "status",
                "jobs",
                "job",
                "plan",
                "stats",
                "health",
                "diag",
                "retry",
                "cancel",
                "cache",
                "archive",
                "help",
            },
        )
        self.assertFalse(
            names & {"queue", "begin", "end", "profiles", "webdav", "backup", "dashboard"}
        )

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

