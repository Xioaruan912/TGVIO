from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.adapters.telegram.message_remover import TelethonPublishedMessageRemover
from tgvio.application.operation_tokens import (
    OperationTokenInvalidError,
    OperationTokenService,
)
from tgvio.application.undo import UndoOperationInvalidError, UndoService
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.operations import RevocationState
from tgvio.domain.publish import (
    PublishEffect,
    PublishPlan,
    PublishStep,
    PublishStepKind,
    PublishStepState,
    PublishTarget,
)
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class FakeRemover:
    def __init__(self, *, fail_once: set[tuple[int, int]] | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.fail_once = set(fail_once or ())

    async def delete_message(self, peer_id: int, message_id: int) -> None:
        key = (peer_id, message_id)
        self.calls.append(key)
        if key in self.fail_once:
            self.fail_once.remove(key)
            raise TimeoutError("fixture timeout")


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[int, ...], bool]] = []

    async def delete_messages(self, entity, message_ids, *, revoke=False):
        self.calls.append((int(entity), tuple(int(value) for value in message_ids), bool(revoke)))
        return True


class HangingRemover:
    def __init__(self) -> None:
        self.calls = 0

    async def delete_message(self, peer_id: int, message_id: int) -> None:
        self.calls += 1
        await asyncio.Event().wait()


class UndoServiceTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_confirm_is_owner_scoped_single_use_and_ignores_commit_marker(self) -> None:
        await self._record_effect(peer_id=-1001, message_id=11)
        await self._record_effect(
            peer_id=-1002,
            message_id=22,
            effect_type="telegram_discussion_message",
        )
        await self.repo.record_publish_effect(
            PublishEffect(
                plan_id=self.plan.id,
                step_index=0,
                effect_type="publish_step_receipts_committed",
                detail={"count": 2},
            )
        )
        remover = FakeRemover()
        service = UndoService(self.repo, remover, token_factory=lambda: "undo-token-one")

        confirmation = await service.prepare(self.job, owner_id=42)
        self.assertEqual(confirmation.status.total_messages, 2)
        self.assertEqual(confirmation.status.channel_messages, 1)
        self.assertEqual(confirmation.status.discussion_messages, 1)
        self.assertEqual(confirmation.status.remaining_messages, 2)
        self.assertEqual(confirmation.operation.owner_id, 42)
        self.assertEqual(confirmation.operation.action, "undo_publish")
        self.assertNotIn("@channel", str(confirmation.operation.payload))

        with self.assertRaises(UndoOperationInvalidError):
            await service.confirm(owner_id=7, token=confirmation.operation.token)
        self.assertEqual(remover.calls, [])

        result = await service.confirm(owner_id=42, token=confirmation.operation.token)
        self.assertTrue(result.complete)
        self.assertEqual(result.deleted_total, 2)
        self.assertEqual(remover.calls, [(-1002, 22), (-1001, 11)])
        revocations = await self.repo.list_publish_effect_revocations(self.job.id)
        self.assertEqual({item.state for item in revocations}, {RevocationState.DELETED})
        events = await self.repo.list_publish_effect_revocation_events(self.job.id)
        self.assertEqual([event["event_type"] for event in events], ["delete_succeeded", "delete_succeeded"])

        with self.assertRaises(UndoOperationInvalidError):
            await service.confirm(owner_id=42, token=confirmation.operation.token)
        self.assertEqual(len(remover.calls), 2)

    async def test_partial_failure_reissues_token_for_remaining_message_only(self) -> None:
        await self._record_effect(peer_id=-1001, message_id=11)
        await self._record_effect(
            peer_id=-1002,
            message_id=22,
            effect_type="telegram_discussion_message",
        )
        tokens = iter(("undo-token-a", "undo-token-b"))
        remover = FakeRemover(fail_once={(-1002, 22)})
        service = UndoService(
            self.repo,
            remover,
            token_factory=lambda: next(tokens),
            delete_attempts=1,
        )

        first = await service.prepare(self.job, owner_id=42)
        result = await service.confirm(owner_id=42, token=first.operation.token)
        self.assertFalse(result.complete)
        self.assertEqual(result.deleted_now, 1)
        self.assertEqual(result.failed_now, 1)
        self.assertEqual(result.remaining_messages, 1)

        second = await service.prepare(self.job, owner_id=42)
        self.assertEqual(second.status.remaining_messages, 1)
        self.assertEqual(second.status.remaining_targets[0].peer_id, -1002)
        result = await service.confirm(owner_id=42, token=second.operation.token)
        self.assertTrue(result.complete)
        self.assertEqual(remover.calls.count((-1001, 11)), 1)
        self.assertEqual(remover.calls.count((-1002, 22)), 2)
        events = await self.repo.list_publish_effect_revocation_events(self.job.id)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["delete_failed", "delete_succeeded", "delete_succeeded"],
        )

    async def test_transient_delete_failure_is_retried_and_each_attempt_is_audited(self) -> None:
        await self._record_effect(peer_id=-1001, message_id=11)
        remover = FakeRemover(fail_once={(-1001, 11)})
        service = UndoService(
            self.repo,
            remover,
            token_factory=lambda: "retry-token",
            delete_retry_delay_seconds=0,
        )

        confirmation = await service.prepare(self.job, owner_id=42)
        result = await service.confirm(owner_id=42, token=confirmation.operation.token)

        self.assertTrue(result.complete)
        self.assertEqual(remover.calls, [(-1001, 11), (-1001, 11)])
        events = await self.repo.list_publish_effect_revocation_events(self.job.id)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["delete_failed", "delete_succeeded"],
        )

    async def test_hanging_delete_is_bounded_and_left_for_safe_resume(self) -> None:
        await self._record_effect(peer_id=-1001, message_id=11)
        remover = HangingRemover()
        service = UndoService(
            self.repo,
            remover,
            token_factory=lambda: "timeout-token",
            delete_timeout_seconds=0.01,
            delete_attempts=1,
        )

        confirmation = await service.prepare(self.job, owner_id=42)
        result = await asyncio.wait_for(
            service.confirm(owner_id=42, token=confirmation.operation.token),
            timeout=0.5,
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.remaining_messages, 1)
        self.assertEqual(remover.calls, 1)
        revocations = await self.repo.list_publish_effect_revocations(self.job.id)
        self.assertEqual(revocations[0].state, RevocationState.FAILED)
        self.assertEqual(revocations[0].error_code, "telegram_delete_timeout")

    async def test_repeated_transport_failures_open_circuit_before_long_batch_stall(self) -> None:
        for message_id in range(11, 15):
            await self._record_effect(peer_id=-1001, message_id=message_id)
        remover = HangingRemover()
        service = UndoService(
            self.repo,
            remover,
            token_factory=lambda: "circuit-token",
            delete_timeout_seconds=0.01,
            delete_attempts=1,
            max_consecutive_failures=2,
        )

        confirmation = await service.prepare(self.job, owner_id=42)
        result = await asyncio.wait_for(
            service.confirm(owner_id=42, token=confirmation.operation.token),
            timeout=0.5,
        )

        self.assertEqual(remover.calls, 2)
        self.assertEqual(result.failed_now, 2)
        self.assertEqual(result.remaining_messages, 4)

    async def test_new_publish_effect_invalidates_prepared_operation(self) -> None:
        await self._record_effect(peer_id=-1001, message_id=11)
        remover = FakeRemover()
        service = UndoService(self.repo, remover, token_factory=lambda: "stale-token")
        confirmation = await service.prepare(self.job, owner_id=42)

        await self._record_effect(peer_id=-1001, message_id=12)
        with self.assertRaises(UndoOperationInvalidError):
            await service.confirm(owner_id=42, token=confirmation.operation.token)
        self.assertEqual(remover.calls, [])

    async def test_duplicate_effect_target_is_deleted_once_and_all_effects_checkpoint(self) -> None:
        first = await self._record_effect(peer_id=-1001, message_id=11)
        second = await self._record_effect(peer_id=-1001, message_id=11)
        remover = FakeRemover()
        service = UndoService(self.repo, remover, token_factory=lambda: "dedupe-token")

        confirmation = await service.prepare(self.job, owner_id=42)
        self.assertEqual(confirmation.status.total_messages, 1)
        result = await service.confirm(owner_id=42, token=confirmation.operation.token)
        self.assertTrue(result.complete)
        self.assertEqual(remover.calls, [(-1001, 11)])
        revocations = await self.repo.list_publish_effect_revocations(self.job.id)
        self.assertEqual(
            {item.effect_id for item in revocations},
            {int(first.id or 0), int(second.id or 0)},
        )
        self.assertTrue(all(item.state == RevocationState.DELETED for item in revocations))

    async def test_repository_rejects_cross_job_revocation_checkpoint(self) -> None:
        effect = await self._record_effect(peer_id=-1001, message_id=11)
        foreign = Job(
            id="c" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            items=[],
        )
        await self.repo.create(foreign)
        with self.assertRaises(ValueError):
            await self.repo.ensure_publish_effect_revocations(
                foreign.id,
                (int(effect.id or 0),),
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
