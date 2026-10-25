from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.adapters.telegram.message_remover import TelethonPublishedMessageRemover
from tgvio.application.operation_tokens import (
    OperationTokenInvalidError,
    OperationTokenService,
)
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.publish import (
    PublishEffect,
    PublishPlan,
    PublishStep,
    PublishStepKind,
    PublishStepState,
    PublishTarget,
)
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[int, ...], bool]] = []

    async def delete_messages(self, entity, message_ids, *, revoke=False):
        self.calls.append((int(entity), tuple(int(value) for value in message_ids), bool(revoke)))
        return True


class OperationTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.job = Job(
            id="a" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
        )
        await self.repo.create(self.job)
        self.plan = PublishPlan(
            id="b" * 32,
            job_id=self.job.id,
            steps=(
                PublishStep(
                    index=0,
                    kind=PublishStepKind.CHANNEL_VIDEO_COVER,
                    target=PublishTarget.CHANNEL,
                    item_indexes=(0,),
                    state=PublishStepState.SUCCEEDED,
                ),
            ),
            summary={},
        )
        await self.repo.save_publish_plan(self.plan)

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _record_effect(
        self,
        *,
        peer_id: int,
        message_id: int,
        effect_type: str = "telegram_channel_message",
    ) -> PublishEffect:
        return await self.repo.record_publish_effect(
            PublishEffect(
                plan_id=self.plan.id,
                step_index=0,
                effect_type=effect_type,
                external_chat_id=str(peer_id),
                external_message_id=str(message_id),
            )
        )

    async def test_consuming_one_operation_invalidates_sibling_confirmation(self) -> None:
        first = await self.repo.create_operation_token(
            token="sibling-one",
            owner_id=42,
            action="fixture_publish",
            resource_type="job",
            resource_id=self.job.id,
            expected_revision=2,
            payload_hash="b" * 64,
            payload={"version": 1},
            ttl_seconds=300,
        )
        second = await self.repo.create_operation_token(
            token="sibling-two",
            owner_id=42,
            action="fixture_publish",
            resource_type="job",
            resource_id=self.job.id,
            expected_revision=2,
            payload_hash="b" * 64,
            payload={"version": 1},
            ttl_seconds=300,
        )

        consumed = await self.repo.consume_operation_token(
            token=first.token,
            owner_id=42,
            action="fixture_publish",
            resource_type="job",
            resource_id=self.job.id,
            expected_revision=2,
            payload_hash="b" * 64,
        )
        self.assertIsNotNone(consumed)
        sibling = await self.repo.get_operation_token(second.token)
        self.assertIsNotNone(sibling)
        assert sibling is not None
        self.assertIsNotNone(sibling.consumed_at)
        self.assertIsNone(
            await self.repo.consume_operation_token(
                token=second.token,
                owner_id=42,
                action="fixture_publish",
                resource_type="job",
                resource_id=self.job.id,
                expected_revision=2,
                payload_hash="b" * 64,
            )
        )

    async def test_expired_operation_token_cannot_be_consumed(self) -> None:
        operation = await self.repo.create_operation_token(
            token="expired-token",
            owner_id=42,
            action="undo_publish",
            resource_type="job",
            resource_id=self.job.id,
            expected_revision=1,
            payload_hash="a" * 64,
            payload={"version": 1},
            ttl_seconds=1,
        )
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                "UPDATE operation_tokens SET expires_at=0 WHERE token=?",
                (operation.token,),
            )
        consumed = await self.repo.consume_operation_token(
            token=operation.token,
            owner_id=42,
            action="undo_publish",
            resource_type="job",
            resource_id=self.job.id,
            expected_revision=1,
            payload_hash="a" * 64,
        )
        self.assertIsNone(consumed)

    async def test_expired_operation_token_cannot_be_inspected(self) -> None:
        service = OperationTokenService(self.repo, token_factory=lambda: "inspect-expired")
        operation = await service.issue(
            owner_id=42,
            action="cancel_job",
            resource_type="job",
            resource_id=self.job.id,
            expected_revision=1,
            payload={"job_id": self.job.id},
        )
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                "UPDATE operation_tokens SET expires_at=0 WHERE token=?",
                (operation.token,),
            )

        with self.assertRaises(OperationTokenInvalidError):
            await service.inspect(token=operation.token, owner_id=42, action="cancel_job")


class TelethonPublishedMessageRemoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_is_peer_scoped_and_revoked(self) -> None:
        client = FakeTelegramClient()
        remover = TelethonPublishedMessageRemover(client)  # type: ignore[arg-type]

        await remover.delete_message(-100123, 456)

        self.assertEqual(client.calls, [(-100123, (456,), True)])


if __name__ == "__main__":
    unittest.main()
