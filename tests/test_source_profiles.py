import tempfile
import unittest
from pathlib import Path

from src.repository import SQLiteRepository
from src.services import SourceProfileManager


class SourceProfileTests(unittest.IsolatedAsyncioTestCase):
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
        self.destination = await self.repo.ensure_env_destination_profile(
            destination_peer="@dest",
            channel_at="@dest",
        )
        self.manager = SourceProfileManager(self.repo)

    async def test_source_profile_starts_disabled_and_verified_can_enable(self) -> None:
        profile = await self.manager.create_verified(
            name="来源一",
            source_peer="@source",
            source_peer_id=-100123,
            destination_profile_id=self.destination.id,
            owner_user_id=42,
        )
        self.assertFalse(profile.enabled)
        self.assertIsNotNone(profile.verified_at)
        self.assertEqual(await self.manager.set_enabled(profile.id, True), "ok")
        enabled = await self.manager.get_enabled_by_peer(-100123)
        self.assertIsNotNone(enabled)
        self.assertEqual(enabled.destination_profile_id, self.destination.id)

    async def test_unverified_profile_cannot_enable(self) -> None:
        profile = await self.repo.create_source_profile(
            name="未验证",
            source_peer="@unverified",
            source_peer_id=-100124,
            destination_profile_id=self.destination.id,
            owner_user_id=42,
        )
        self.assertEqual(await self.manager.set_enabled(profile.id, True), "unverified")

    async def test_source_event_is_idempotent_by_peer_and_message(self) -> None:
        profile = await self.manager.create_verified(
            name="来源二",
            source_peer="@source2",
            source_peer_id=-100125,
            destination_profile_id=self.destination.id,
            owner_user_id=42,
        )
        first, inserted = await self.manager.accept_event(
            profile=profile,
            source_message_id=77,
            grouped_id=900,
        )
        duplicate, inserted_again = await self.manager.accept_event(
            profile=profile,
            source_message_id=77,
            grouped_id=900,
        )
        self.assertTrue(inserted)
        self.assertFalse(inserted_again)
        self.assertEqual(first.id, duplicate.id)
        await self.manager.mark_enqueued([first.id], 501)
        conn = self.repo._require_conn()
        row = await (await conn.execute("SELECT state,job_legacy_seq FROM source_events WHERE id=?", (first.id,))).fetchone()
        self.assertEqual((row["state"], row["job_legacy_seq"]), ("enqueued", 501))

    async def test_source_policy_validation(self) -> None:
        profile = await self.manager.create_verified(
            name="策略",
            source_peer="@policy",
            source_peer_id=-100126,
            destination_profile_id=self.destination.id,
            owner_user_id=42,
        )
        self.assertEqual(await self.manager.update(profile.id, spoiler_policy="spoiler"), "ok")
        self.assertEqual(await self.manager.update(profile.id, caption_policy="strip"), "ok")
        self.assertEqual(await self.manager.update(profile.id, backup_policy="required"), "ok")
        current = await self.manager.get(profile.id)
        self.assertEqual((current.spoiler_policy, current.caption_policy, current.backup_policy), ("spoiler", "strip", "required"))

    async def test_enabled_source_protects_destination_from_disable(self) -> None:
        custom = await self.repo.create_destination_profile(
            name="自动目的地",
            destination_peer="@auto-dest",
        )
        source = await self.manager.create_verified(
            name="保护来源",
            source_peer="@protected-source",
            source_peer_id=-100127,
            destination_profile_id=custom.id,
            owner_user_id=42,
        )
        self.assertEqual(await self.manager.set_enabled(source.id, True), "ok")
        self.assertEqual(await self.repo.disable_destination_profile(custom.id), "source_in_use")
        self.assertEqual(await self.manager.set_enabled(source.id, False), "ok")
        self.assertEqual(await self.repo.disable_destination_profile(custom.id), "ok")

    async def test_received_source_events_close_as_interrupted_on_restart(self) -> None:
        profile = await self.manager.create_verified(
            name="重启来源",
            source_peer="@restart-source",
            source_peer_id=-100128,
            destination_profile_id=self.destination.id,
            owner_user_id=42,
        )
        record, _ = await self.manager.accept_event(
            profile=profile,
            source_message_id=88,
            grouped_id=901,
        )
        self.assertEqual(await self.repo.interrupt_received_source_events(), 1)
        conn = self.repo._require_conn()
        row = await (await conn.execute("SELECT state,error_code FROM source_events WHERE id=?", (record.id,))).fetchone()
        self.assertEqual((row["state"], row["error_code"]), ("interrupted", "process_restart"))


if __name__ == "__main__":
    unittest.main()
