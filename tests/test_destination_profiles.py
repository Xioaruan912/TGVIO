import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.media import MediaPublisher
from src.handlers.settings import callback_destination_profile
from src.models import Job, PendingJob
from src.repository import SQLiteRepository
from src.services import DestinationProfileManager, InteractionSessions, JobQueue, OperationStore, ShadowState
from tests.fakes import FakeCallbackEvent, FakeClient, FakeStatusMessage


class DestinationProfileManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        downloads = root / "downloads"
        downloads.mkdir()
        self.repo = SQLiteRepository(root / "state.sqlite3", download_root=downloads)
        await self.repo.open()
        await self.repo.migrate()
        self.addAsyncCleanup(self.repo.close)
        default = await self.repo.ensure_env_destination_profile(
            destination_peer="@default", channel_at="@default"
        )
        self.manager = DestinationProfileManager(self.repo, default)

    async def test_default_switch_changes_future_snapshot_not_existing_job_snapshot(self) -> None:
        first_snapshot = self.manager.current_snapshot()
        job = await self.repo.accept_job(
            kind="url", user_id=1, state="queued", source_kind="url", legacy_seq=1001,
            destination_profile_id=self.manager.current_profile.id,
            destination_profile_snapshot=first_snapshot,
            event_payload={"schema_version": 1},
        )
        custom = await self.manager.create_profile(name="第二频道", destination_peer="@second")
        self.assertFalse(await self.manager.set_default(custom.id))
        self.assertTrue(await self.manager.mark_verified(custom.id))
        self.assertTrue(await self.manager.set_default(custom.id))
        self.assertEqual(self.manager.current_snapshot()["destination_peer"], "@second")
        stored = __import__("json").loads((await self.repo.get_job(job.id)).destination_profile_snapshot_json)
        self.assertEqual(stored["destination_peer"], "@default")

    async def test_routing_edit_invalidates_verification_until_retested(self) -> None:
        profile = await self.manager.create_profile(name="可验证频道", destination_peer="@first")
        self.assertTrue(await self.manager.mark_verified(profile.id))
        self.assertTrue(await self.manager.set_default(profile.id))
        self.assertEqual(
            await self.manager.update_profile(profile.id, destination_peer="@blocked"),
            "default_requires_switch",
        )
        env = next(item for item in await self.manager.list_profiles() if item.source_kind == "env")
        self.assertTrue(await self.manager.set_default(env.id))
        self.assertEqual(
            await self.manager.update_profile(profile.id, destination_peer="@second"),
            "ok",
        )
        refreshed = await self.repo.get_destination_profile(profile.id)
        self.assertIsNone(refreshed.verified_at)
        self.assertFalse(await self.manager.set_default(profile.id))

    async def test_pending_profile_selection_cas_updates_durable_snapshot(self) -> None:
        custom = await self.manager.create_profile(name="待选频道", destination_peer="@pending")
        seq = 1201
        initial = self.manager.current_profile
        job = await self.repo.accept_job(
            kind="media",
            user_id=42,
            state="awaiting_confirmation",
            source_kind="telegram",
            legacy_seq=seq,
            destination_profile_id=initial.id,
            destination_profile_snapshot=self.manager.current_snapshot(),
            event_payload={"schema_version": 1},
        )
        pending = PendingJob(
            seq=seq,
            kind="media",
            user_id=42,
            destination_profile_id=initial.id,
            destination_profile_name=initial.name,
            destination_profile_snapshot=self.manager.current_snapshot(),
        )
        pipeline = SimpleNamespace(
            repository=self.repo,
            pending={seq: pending},
            destination_profiles=self.manager,
        )
        shadow = ShadowState(self.repo)
        shadow.job_ids[seq] = job.id
        queue = JobQueue(pipeline, shadow=shadow)
        self.assertEqual(
            await queue.select_pending_destination(seq, custom.id, user_id=42),
            "ok",
        )
        current = await self.repo.get_job(job.id)
        snapshot = __import__("json").loads(current.destination_profile_snapshot_json)
        self.assertEqual(snapshot["destination_peer"], "@pending")
        self.assertEqual(pending.destination_profile_id, custom.id)

    async def test_profile_test_button_has_no_send_side_effect_before_confirmation(self) -> None:
        profile = await self.manager.create_profile(name="待测试频道", destination_peer="@test")
        client = FakeClient()

        class _Ctx:
            destinations = self.manager
            operations = OperationStore()
            interactions = InteractionSessions()

            @staticmethod
            async def answer(event, text=""):
                await event.answer(text)

            @staticmethod
            async def edit(event, text, **kwargs):
                await event.edit(text, **kwargs)

        event = FakeCallbackEvent(client, f"dp:t:{profile.id}".encode(), sender_id=42)
        await callback_destination_profile(_Ctx(), event, f"dp:t:{profile.id}")
        self.assertEqual(client.sent_messages, [])
        self.assertTrue(event.edits)
        callbacks = [
            button.data
            for row in event.edits[-1]["buttons"]
            for button in row
        ]
        self.assertTrue(any(data.startswith(b"dp:tc:") for data in callbacks))
        self.assertTrue(all(len(data) <= 64 for data in callbacks))

    def test_footer_template_is_whitelisted_and_not_expression_evaluated(self) -> None:
        self.manager.validate_footer_template("{profile_name} {channel_at} {group_at}")
        with self.assertRaises(ValueError):
            self.manager.validate_footer_template("{__class__}")
        with self.assertRaises(ValueError):
            self.manager.validate_footer_template("{channel_at.upper()}")


class DestinationProfilePublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_uses_job_profile_then_restores_base_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "item.bin"
            path.write_bytes(b"payload")
            client = FakeClient()
            publisher = MediaPublisher(
                client,
                "@base",
                lambda _seq: tmp,
                upload_timeout=30,
                max_file_size=1024,
                forward_caption=False,
                cover_mode=False,
            )
            publisher._upload_media_input = AsyncMock(return_value="uploaded")
            publisher._get_dest_input = AsyncMock(return_value="profile-input")

            async def configure(job, _payload):
                snapshot = {
                    "destination_peer": "@profile",
                    "cover_mode": False,
                    "forward_caption": True,
                    "channel_at": "@profile",
                    "group_at": "",
                }
                publisher.configure_destination_profile(snapshot, footer="@profile")
                job._destination_key = "@profile"

            publisher.pre_publish_hooks.append(configure)
            job = Job(
                seq=77,
                kind="media",
                status=FakeStatusMessage(),
                message=SimpleNamespace(message="new caption"),
            )
            await publisher.publish(job, str(path))
            self.assertEqual(client.sent_files[0]["peer"], "@profile")
            self.assertIn("new caption", client.sent_files[0]["caption"])
            self.assertEqual(publisher.dest, "@base")


if __name__ == "__main__":
    unittest.main()
