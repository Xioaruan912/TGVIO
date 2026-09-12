from __future__ import annotations

import asyncio
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tgvio.application.scheduler import (
    OrderedPublishDispatcher,
    PhaseClaimGuard,
    PhaseClaimLostError,
    RuntimeLeaseGuard,
)
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class _RecordingRunner:
    def __init__(self, repository: SQLiteJobRepository, *, delay: float = 0.0) -> None:
        self.repository = repository
        self.publish_enabled = True
        self.published: list[str] = []
        self.delay = delay

    async def publish(self, job: Job) -> Job:
        current = await self.repository.get(job.id)
        assert current is not None
        current.state = JobState.PUBLISHING
        await self.repository.save(current)
        await asyncio.sleep(self.delay)
        current.state = JobState.SUCCEEDED
        await self.repository.save(current)
        self.published.append(job.id)
        return current


class DurableSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "state.sqlite3"
        self.repo = SQLiteJobRepository(self.database, backup_dir=root / "backups")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.temporary.cleanup()

    async def _create_job(self, index: int) -> Job:
        job = Job(
            id=f"job-{index:03d}",
            owner_id=42,
            destination="@channel",
            items=[MediaItem(index=0, kind=MediaKind.VIDEO, source=f"source-{index}")],
        )
        await self.repo.create(job)
        return job

    async def test_runtime_lease_is_singleton_and_generation_fenced(self) -> None:
        first = await self.repo.acquire_runtime_lease(
            "telegram-runtime", "holder-a", ttl_seconds=30
        )
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.generation, 1)
        blocked = await self.repo.acquire_runtime_lease(
            "telegram-runtime", "holder-b", ttl_seconds=30
        )
        self.assertIsNone(blocked)
        self.assertTrue(await self.repo.release_runtime_lease(first))
        second = await self.repo.acquire_runtime_lease(
            "telegram-runtime", "holder-b", ttl_seconds=30
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.generation, 2)
        self.assertIsNone(
            await self.repo.heartbeat_runtime_lease(first, ttl_seconds=30)
        )
        self.assertIsNotNone(
            await self.repo.heartbeat_runtime_lease(second, ttl_seconds=30)
        )

    async def test_runtime_lease_is_atomic_across_repository_connections(self) -> None:
        second_repo = SQLiteJobRepository(self.database)
        await second_repo.open()
        try:
            first, second = await asyncio.gather(
                self.repo.acquire_runtime_lease(
                    "telegram-runtime", "runtime-a", ttl_seconds=30
                ),
                second_repo.acquire_runtime_lease(
                    "telegram-runtime", "runtime-b", ttl_seconds=30
                ),
            )
            acquired = [lease for lease in (first, second) if lease is not None]
            self.assertEqual(len(acquired), 1)
        finally:
            await second_repo.close()

    async def test_runtime_lease_guard_rejects_second_runtime(self) -> None:
        first = RuntimeLeaseGuard(self.repo, ttl_seconds=30, holder_id="runtime-a")
        second = RuntimeLeaseGuard(self.repo, ttl_seconds=30, holder_id="runtime-b")
        await first.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "singleton lease"):
                await second.start()
        finally:
            await first.stop()

    async def test_phase_claim_prevents_duplicate_and_stale_generation(self) -> None:
        job = await self._create_job(1)
        first = await self.repo.acquire_phase_claim(
            job.id, "prepare", "worker-a", ttl_seconds=30
        )
        self.assertIsNotNone(first)
        assert first is not None
        self.assertIsNone(
            await self.repo.acquire_phase_claim(
                job.id, "prepare", "worker-b", ttl_seconds=30
            )
        )
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE job_phase_claims SET expires_at=0 WHERE job_id=? AND phase='prepare'",
            (job.id,),
        )
        connection.commit()
        connection.close()
        second = await self.repo.acquire_phase_claim(
            job.id, "prepare", "worker-b", ttl_seconds=30
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.generation, first.generation + 1)
        self.assertIsNone(
            await self.repo.heartbeat_phase_claim(first, ttl_seconds=30)
        )
        self.assertIsNotNone(
            await self.repo.heartbeat_phase_claim(second, ttl_seconds=30)
        )

    async def test_phase_claim_is_atomic_across_repository_connections(self) -> None:
        job = await self._create_job(1)
        second_repo = SQLiteJobRepository(self.database)
        await second_repo.open()
        try:
            first, second = await asyncio.gather(
                self.repo.acquire_phase_claim(
                    job.id, "prepare", "worker-a", ttl_seconds=30
                ),
                second_repo.acquire_phase_claim(
                    job.id, "prepare", "worker-b", ttl_seconds=30
                ),
            )
            acquired = [claim for claim in (first, second) if claim is not None]
            self.assertEqual(len(acquired), 1)
        finally:
            await second_repo.close()

    async def test_global_pause_blocks_new_claim_but_not_existing_heartbeat(self) -> None:
        first_job = await self._create_job(1)
        second_job = await self._create_job(2)
        first = await self.repo.acquire_phase_claim(
            first_job.id, "prepare", "worker-a", ttl_seconds=30
        )
        self.assertIsNotNone(first)
        assert first is not None
        paused = await self.repo.set_queue_paused(True, reason="maintenance")
        self.assertTrue(paused.paused)
        self.assertIsNotNone(await self.repo.heartbeat_phase_claim(first, ttl_seconds=30))
        self.assertIsNone(
            await self.repo.acquire_phase_claim(
                second_job.id, "prepare", "worker-b", ttl_seconds=30
            )
        )
        await self.repo.set_queue_paused(False)
        self.assertIsNotNone(
            await self.repo.acquire_phase_claim(
                second_job.id, "prepare", "worker-b", ttl_seconds=30
            )
        )

    async def test_held_job_cannot_claim_and_is_skipped_by_publish_gate(self) -> None:
        first = await self._create_job(1)
        second = await self._create_job(2)
        first.state = JobState.PLANNED
        second.state = JobState.PLANNED
        await self.repo.save(first)
        await self.repo.save(second)
        await self.repo.request_hold(first.id, reason="operator pause")

        self.assertIsNone(
            await self.repo.acquire_phase_claim(
                first.id, "publish", "worker-a", ttl_seconds=30
            )
        )
        gate = await self.repo.get_next_publish_gate()
        assert gate is not None
        self.assertEqual(gate.job_id, second.id)

        await self.repo.clear_hold(first.id)
        gate = await self.repo.get_next_publish_gate()
        assert gate is not None
        self.assertEqual(gate.job_id, first.id)

    async def test_held_head_job_allows_explainable_overtake_then_resumes(self) -> None:
        first = await self._create_job(1)
        second = await self._create_job(2)
        first.state = JobState.PLANNED
        second.state = JobState.PLANNED
        await self.repo.save(first)
        await self.repo.save(second)
        await self.repo.request_hold(first.id, reason="operator pause")
        runner = _RecordingRunner(self.repo)
        dispatcher = OrderedPublishDispatcher(self.repo, runner, poll_seconds=0.01)
        await dispatcher.start()
        try:
            dispatcher.notify()
            deadline = asyncio.get_running_loop().time() + 2
            while runner.published != [second.id]:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("later job did not overtake held head job")
                await asyncio.sleep(0.01)
            await self.repo.clear_hold(first.id)
            dispatcher.notify()
            deadline = asyncio.get_running_loop().time() + 2
            while runner.published != [second.id, first.id]:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("resumed held job did not publish")
                await asyncio.sleep(0.01)
        finally:
            await dispatcher.stop()

    async def test_claim_loss_cancels_inflight_operation(self) -> None:
        job = await self._create_job(1)
        guard = PhaseClaimGuard(
            self.repo,
            job.id,
            "prepare",
            ttl_seconds=6,
            holder_id="worker-a",
        )
        self.assertTrue(await guard.start())
        try:
            connection = sqlite3.connect(self.database)
            connection.execute(
                "UPDATE job_phase_claims SET expires_at=0 WHERE job_id=? AND phase='prepare'",
                (job.id,),
            )
            connection.commit()
            connection.close()
            second = await self.repo.acquire_phase_claim(
                job.id,
                "prepare",
                "worker-b",
                ttl_seconds=30,
            )
            self.assertIsNotNone(second)
            with self.assertRaises(PhaseClaimLostError):
                await asyncio.wait_for(guard.run(asyncio.sleep(10)), timeout=4)
        finally:
            await guard.stop()

    async def test_accepted_order_is_monotonic_and_durable(self) -> None:
        jobs = [await self._create_job(index) for index in range(100)]
        orders = [await self.repo.get_accepted_order(job.id) for job in jobs]
        self.assertEqual(orders, list(range(1, 101)))

    async def test_publish_dispatcher_preserves_accept_order_despite_random_readiness(self) -> None:
        jobs = [await self._create_job(index) for index in range(100)]
        runner = _RecordingRunner(self.repo)
        dispatcher = OrderedPublishDispatcher(self.repo, runner, poll_seconds=0.01)
        await dispatcher.start()
        order = list(range(100))
        random.Random(20260912).shuffle(order)
        try:
            for index in order:
                job = await self.repo.get(jobs[index].id)
                assert job is not None
                job.state = JobState.PLANNED
                await self.repo.save(job)
                dispatcher.notify()
                await asyncio.sleep(0)
            deadline = asyncio.get_running_loop().time() + 10
            while len(runner.published) < len(jobs):
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("ordered dispatcher did not publish all jobs")
                await asyncio.sleep(0.01)
        finally:
            await dispatcher.stop()
        self.assertEqual(runner.published, [job.id for job in jobs])

    async def test_later_download_does_not_block_ready_head_job(self) -> None:
        first = await self._create_job(1)
        await self._create_job(2)
        first.state = JobState.PLANNED
        await self.repo.save(first)
        runner = _RecordingRunner(self.repo)
        dispatcher = OrderedPublishDispatcher(self.repo, runner, poll_seconds=0.01)
        await dispatcher.start()
        try:
            dispatcher.notify()
            deadline = asyncio.get_running_loop().time() + 2
            while not runner.published:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("ready head job was blocked by a later download")
                await asyncio.sleep(0.01)
        finally:
            await dispatcher.stop()
        self.assertEqual(runner.published, [first.id])

    async def test_two_dispatchers_cannot_publish_same_job(self) -> None:
        job = await self._create_job(1)
        job.state = JobState.PLANNED
        await self.repo.save(job)
        runner = _RecordingRunner(self.repo, delay=0.05)
        first = OrderedPublishDispatcher(self.repo, runner, poll_seconds=0.005)
        second = OrderedPublishDispatcher(self.repo, runner, poll_seconds=0.005)
        await first.start()
        await second.start()
        try:
            first.notify()
            second.notify()
            deadline = asyncio.get_running_loop().time() + 2
            while len(runner.published) < 1:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("publish dispatcher did not complete")
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
        finally:
            await first.stop()
            await second.stop()
        self.assertEqual(runner.published, [job.id])

    async def test_uncertain_publish_blocks_later_jobs(self) -> None:
        first = await self._create_job(1)
        second = await self._create_job(2)
        first.state = JobState.FAILED
        first.error_code = "publish_uncertain"
        await self.repo.save(first)
        second.state = JobState.PLANNED
        await self.repo.save(second)
        gate = await self.repo.get_next_publish_gate()
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate.job_id, first.id)
        self.assertTrue(gate.blocks_for_manual_review)
        runner = _RecordingRunner(self.repo)
        dispatcher = OrderedPublishDispatcher(self.repo, runner, poll_seconds=0.01)
        with self.assertLogs("tgvio.scheduler.publish", level="WARNING") as captured:
            await dispatcher.start()
            try:
                dispatcher.notify()
                await asyncio.sleep(0.1)
            finally:
                await dispatcher.stop()
        self.assertEqual(len(captured.output), 1)
        self.assertEqual(runner.published, [])
