from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, TypeVar
from uuid import uuid4

from tgvio.application.job_control import JobHoldRequested
from tgvio.application.job_runner import JobRunner
from tgvio.application.ports import JobRepository
from tgvio.domain.job import JobState
from tgvio.domain.scheduler import PhaseClaim, RuntimeLease
from tgvio.observability import log_event


_ResultT = TypeVar("_ResultT")


class PhaseClaimLostError(RuntimeError):
    """The durable generation-fenced claim was lost while work was in flight."""


class RuntimeLeaseGuard:
    """Own the singleton runtime lease and fail closed when the lease is lost."""

    def __init__(
        self,
        repository: JobRepository,
        *,
        lease_name: str = "telegram-runtime",
        ttl_seconds: int = 30,
        holder_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._lease_name = lease_name
        self._ttl_seconds = max(6, int(ttl_seconds))
        self._holder_id = holder_id or uuid4().hex
        self._lease: RuntimeLease | None = None
        self._task: asyncio.Task | None = None
        self._expiry_timer: asyncio.TimerHandle | None = None
        self._lost = asyncio.Event()
        self._log = logging.getLogger("tgvio.scheduler.runtime_lease")

    @property
    def holder_id(self) -> str:
        return self._holder_id

    async def start(self) -> None:
        if self._task is not None:
            return
        lease = await self._repository.acquire_runtime_lease(
            self._lease_name,
            self._holder_id,
            ttl_seconds=self._ttl_seconds,
        )
        if lease is None:
            raise RuntimeError("another TGVIO runtime holds the singleton lease")
        self._lease = lease
        self._lost.clear()
        self._reset_expiry_timer()
        self._task = asyncio.create_task(
            self._heartbeat_loop(),
            name="tgvio-runtime-lease-heartbeat",
        )
        log_event(
            self._log,
            logging.INFO,
            "runtime.lease.acquired",
            generation=lease.generation,
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None
        lease = self._lease
        self._lease = None
        if lease is not None:
            await self._repository.release_runtime_lease(lease)

    async def wait_lost(self) -> None:
        await self._lost.wait()

    async def _heartbeat_loop(self) -> None:
        interval = max(2.0, self._ttl_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            lease = self._lease
            if lease is None:
                return
            try:
                renewed = await self._repository.heartbeat_runtime_lease(
                    lease,
                    ttl_seconds=self._ttl_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "runtime.lease.heartbeat_failed",
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )
                continue
            if renewed is None:
                log_event(
                    self._log,
                    logging.CRITICAL,
                    "runtime.lease.lost",
                    "Singleton runtime lease was lost; runtime must disconnect",
                    generation=lease.generation,
                )
                self._lost.set()
                return
            self._lease = renewed
            self._reset_expiry_timer()

    def _reset_expiry_timer(self) -> None:
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
        self._expiry_timer = asyncio.get_running_loop().call_later(
            float(self._ttl_seconds),
            self._lost.set,
        )


class PhaseClaimGuard:
    """Generation-fenced durable claim for one Job phase."""

    def __init__(
        self,
        repository: JobRepository,
        job_id: str,
        phase: str,
        *,
        ttl_seconds: int = 120,
        holder_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._job_id = job_id
        self._phase = phase
        self._ttl_seconds = max(6, int(ttl_seconds))
        self._holder_id = holder_id or uuid4().hex
        self._claim: PhaseClaim | None = None
        self._task: asyncio.Task | None = None
        self._expiry_timer: asyncio.TimerHandle | None = None
        self._lost = asyncio.Event()
        self._log = logging.getLogger("tgvio.scheduler.phase_claim")

    @property
    def acquired(self) -> bool:
        return self._claim is not None

    async def start(self) -> bool:
        if self._claim is not None:
            return True
        claim = await self._repository.acquire_phase_claim(
            self._job_id,
            self._phase,
            self._holder_id,
            ttl_seconds=self._ttl_seconds,
        )
        if claim is None:
            return False
        self._claim = claim
        self._lost.clear()
        self._reset_expiry_timer()
        self._task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"tgvio-claim-{self._phase}-{self._job_id}",
        )
        return True

    async def run(self, operation: Awaitable[_ResultT]) -> _ResultT:
        if self._claim is None:
            raise RuntimeError("phase claim is not acquired")
        operation_task = asyncio.create_task(operation)
        lost_task = asyncio.create_task(self._lost.wait())
        done, _pending = await asyncio.wait(
            {operation_task, lost_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lost_task in done:
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            raise PhaseClaimLostError(f"durable phase claim lost: {self._phase}")
        lost_task.cancel()
        await asyncio.gather(lost_task, return_exceptions=True)
        return await operation_task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None
        claim = self._claim
        self._claim = None
        if claim is not None:
            await self._repository.release_phase_claim(claim)

    async def _heartbeat_loop(self) -> None:
        interval = max(2.0, self._ttl_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            claim = self._claim
            if claim is None:
                return
            try:
                renewed = await self._repository.heartbeat_phase_claim(
                    claim,
                    ttl_seconds=self._ttl_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    self._log,
                    logging.ERROR,
                    "scheduler.claim.heartbeat_failed",
                    job_id=self._job_id,
                    phase=self._phase,
                    exception_type=type(exc).__name__,
                    exc_info=True,
                )
                continue
            if renewed is None:
                log_event(
                    self._log,
                    logging.ERROR,
                    "scheduler.claim.lost",
                    "Durable phase claim was lost",
                    job_id=self._job_id,
                    phase=self._phase,
                    generation=claim.generation,
                )
                self._lost.set()
                return
            self._claim = renewed
            self._reset_expiry_timer()

    def _reset_expiry_timer(self) -> None:
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
        self._expiry_timer = asyncio.get_running_loop().call_later(
            float(self._ttl_seconds),
            self._lost.set,
        )


class OrderedPublishDispatcher:
    """Publish one durable Job at a time in accepted_order."""

    def __init__(
        self,
        repository: JobRepository,
        runner: JobRunner,
        *,
        poll_seconds: float = 0.25,
        claim_ttl_seconds: int = 120,
    ) -> None:
        self._repository = repository
        self._runner = runner
        self._poll_seconds = max(0.05, float(poll_seconds))
        self._claim_ttl_seconds = max(6, int(claim_ttl_seconds))
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._last_manual_block: tuple[str, int, str | None] | None = None
        self._log = logging.getLogger("tgvio.scheduler.publish")

    async def start(self) -> None:
        if self._task is not None or not self._runner.publish_enabled:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="tgvio-ordered-publish-dispatcher")
        self._wake.set()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping = True
        self._wake.set()
        task = self._task
        self._task = None
        try:
            await asyncio.wait_for(task, timeout=30)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def notify(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopping:
            gate = await self._repository.get_next_publish_gate()
            if gate is None:
                self._last_manual_block = None
                await self._wait()
                continue
            if gate.blocks_for_manual_review:
                block_key = (gate.job_id, gate.accepted_order, gate.error_code)
                if block_key != self._last_manual_block:
                    log_event(
                        self._log,
                        logging.WARNING,
                        "scheduler.publish.blocked_manual_review",
                        job_id=gate.job_id,
                        accepted_order=gate.accepted_order,
                        error_code=gate.error_code,
                    )
                    self._last_manual_block = block_key
                await self._wait()
                continue
            self._last_manual_block = None
            if gate.state not in {JobState.PLANNED, JobState.PUBLISHING}:
                await self._wait()
                continue
            job = await self._repository.get(gate.job_id)
            if job is None:
                await self._wait()
                continue
            claim = PhaseClaimGuard(
                self._repository,
                job.id,
                "publish",
                ttl_seconds=self._claim_ttl_seconds,
            )
            if not await claim.start():
                await self._wait()
                continue
            try:
                log_event(
                    self._log,
                    logging.INFO,
                    "scheduler.publish.started",
                    job_id=job.id,
                    accepted_order=gate.accepted_order,
                    state=job.state.value,
                )
                try:
                    result = await claim.run(self._runner.publish(job))
                except asyncio.CancelledError:
                    raise
                except PhaseClaimLostError:
                    log_event(
                        self._log,
                        logging.WARNING,
                        "scheduler.publish.claim_lost",
                        "Publish execution stopped because its durable claim was lost",
                        job_id=job.id,
                        accepted_order=gate.accepted_order,
                    )
                    continue
                except JobHoldRequested:
                    log_event(
                        self._log,
                        logging.INFO,
                        "scheduler.publish.held",
                        "Publish stopped at a safe boundary because the Job is held",
                        job_id=job.id,
                        accepted_order=gate.accepted_order,
                    )
                    continue
                except Exception as exc:
                    log_event(
                        self._log,
                        logging.ERROR,
                        "scheduler.publish.failed",
                        "Ordered publish execution failed",
                        job_id=job.id,
                        accepted_order=gate.accepted_order,
                        exception_type=type(exc).__name__,
                        exc_info=True,
                    )
                else:
                    log_event(
                        self._log,
                        logging.INFO,
                        "scheduler.publish.completed",
                        job_id=result.id,
                        accepted_order=gate.accepted_order,
                        state=result.state.value,
                    )
            finally:
                await claim.stop()

    async def _wait(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
        except TimeoutError:
            pass
