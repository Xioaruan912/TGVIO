from __future__ import annotations

from collections.abc import Callable

from tgvio.application.operation_tokens import (
    OperationTokenInvalidError,
    OperationTokenService,
)
from tgvio.application.ports import JobRepository, PublishedMessageRemover
from tgvio.domain.job import Job
from tgvio.domain.operations import (
    OperationToken,
    RevocationState,
    UndoConfirmation,
    UndoResult,
    UndoStatus,
    UndoTarget,
)
from tgvio.domain.publish import PublishEffect


class UndoUnavailableError(RuntimeError):
    pass


class UndoOperationInvalidError(RuntimeError):
    pass


class UndoService:
    ACTION = "undo_publish"
    RESOURCE_TYPE = "job"
    _VISIBLE_EFFECT_TYPES = {
        "telegram_channel_message",
        "telegram_discussion_message",
    }

    def __init__(
        self,
        repository: JobRepository,
        remover: PublishedMessageRemover,
        *,
        ttl_seconds: int = 300,
        token_factory: Callable[[], str] | None = None,
        operation_tokens: OperationTokenService | None = None,
    ) -> None:
        self._repository = repository
        self._remover = remover
        self._operations = operation_tokens or OperationTokenService(
            repository,
            ttl_seconds=ttl_seconds,
            token_factory=token_factory,
        )

    async def status(self, job: Job) -> UndoStatus:
        effects = await self._visible_effects(job)
        return await self._build_status(job.id, effects)

    async def prepare(self, job: Job, *, owner_id: int) -> UndoConfirmation:
        if int(job.owner_id) != int(owner_id):
            raise UndoUnavailableError("job owner mismatch")
        if not job.terminal:
            raise UndoUnavailableError("job is not terminal")
        effects = await self._visible_effects(job)
        if not effects:
            raise UndoUnavailableError("job has no confirmed Telegram messages")
        effect_ids = tuple(int(effect.id) for effect in effects if effect.id is not None)
        await self._repository.ensure_publish_effect_revocations(job.id, effect_ids)
        status = await self._build_status(job.id, effects)
        if status.complete:
            raise UndoUnavailableError("published messages are already revoked")
        if not status.remaining_targets:
            raise UndoUnavailableError("job has no revocable Telegram messages")
        payload = self._payload(job.id, status.remaining_targets)
        operation = await self._operations.issue(
            owner_id=owner_id,
            action=self.ACTION,
            resource_type=self.RESOURCE_TYPE,
            resource_id=job.id,
            expected_revision=status.expected_revision,
            payload=payload,
        )
        return UndoConfirmation(operation=operation, status=status)

    async def confirm(self, *, owner_id: int, token: str) -> UndoResult:
        try:
            operation = await self._operations.inspect(
                token=token,
                owner_id=owner_id,
                action=self.ACTION,
            )
        except OperationTokenInvalidError as exc:
            raise UndoOperationInvalidError("operation is unavailable") from exc
        if operation.resource_type != self.RESOURCE_TYPE:
            raise UndoOperationInvalidError("operation resource is invalid")

        job = await self._repository.get(operation.resource_id)
        if job is None or int(job.owner_id) != int(owner_id) or not job.terminal:
            raise UndoOperationInvalidError("job state changed")

        effects = await self._visible_effects(job)
        if not effects:
            raise UndoOperationInvalidError("published effects are unavailable")
        effect_ids = tuple(int(effect.id) for effect in effects if effect.id is not None)
        await self._repository.ensure_publish_effect_revocations(job.id, effect_ids)
        status = await self._build_status(job.id, effects)
        if (
            status.expected_revision != operation.expected_revision
            or status.payload_hash != operation.payload_hash
        ):
            raise UndoOperationInvalidError("published effects changed")

        try:
            await self._operations.consume(
                token=operation.token,
                owner_id=owner_id,
                action=self.ACTION,
                resource_type=self.RESOURCE_TYPE,
                resource_id=job.id,
                expected_revision=status.expected_revision,
                payload=self._payload(job.id, status.remaining_targets),
            )
        except OperationTokenInvalidError as exc:
            raise UndoOperationInvalidError("operation expired or was already consumed") from exc

        deleted_now = 0
        failed_now = 0
        for target in status.remaining_targets:
            try:
                await self._remover.delete_message(target.peer_id, target.message_id)
            except Exception as exc:
                failed_now += 1
                await self._repository.checkpoint_publish_effect_revocations(
                    job.id,
                    target.effect_ids,
                    state=RevocationState.FAILED,
                    error_code=self._delete_error_code(exc),
                )
            else:
                deleted_now += 1
                await self._repository.checkpoint_publish_effect_revocations(
                    job.id,
                    target.effect_ids,
                    state=RevocationState.DELETED,
                )

        final = await self._build_status(job.id, effects)
        return UndoResult(
            job_id=job.id,
            total_messages=final.total_messages,
            deleted_now=deleted_now,
            deleted_total=final.deleted_messages,
            failed_now=failed_now,
            remaining_messages=final.remaining_messages,
        )

    async def _visible_effects(self, job: Job) -> tuple[PublishEffect, ...]:
        plan = await self._repository.get_publish_plan(job.id)
        if plan is None:
            return ()
        effects = await self._repository.list_publish_effects(plan.id)
        visible: list[PublishEffect] = []
        for effect in effects:
            if (
                effect.id is None
                or effect.effect_type not in self._VISIBLE_EFFECT_TYPES
                or effect.external_chat_id is None
                or effect.external_message_id is None
            ):
                continue
            try:
                int(effect.external_chat_id)
                int(effect.external_message_id)
            except (TypeError, ValueError):
                continue
            visible.append(effect)
        return tuple(visible)

    async def _build_status(
        self,
        job_id: str,
        effects: tuple[PublishEffect, ...],
    ) -> UndoStatus:
        revocations = {
            item.effect_id: item
            for item in await self._repository.list_publish_effect_revocations(job_id)
        }
        grouped: dict[tuple[int, int], list[PublishEffect]] = {}
        for effect in effects:
            assert effect.id is not None
            peer_id = int(effect.external_chat_id or 0)
            message_id = int(effect.external_message_id or 0)
            grouped.setdefault((peer_id, message_id), []).append(effect)

        remaining: list[UndoTarget] = []
        deleted = 0
        failed = 0
        channel = 0
        discussion = 0
        remaining_channel = 0
        remaining_discussion = 0
        all_effect_ids: list[int] = []
        for (peer_id, message_id), group in sorted(
            grouped.items(), key=lambda item: min(int(effect.id or 0) for effect in item[1])
        ):
            ids = tuple(sorted(int(effect.id) for effect in group if effect.id is not None))
            all_effect_ids.extend(ids)
            effect_type = group[0].effect_type
            if effect_type == "telegram_channel_message":
                channel += 1
            else:
                discussion += 1
            states = [revocations[effect_id].state for effect_id in ids if effect_id in revocations]
            if RevocationState.DELETED in states:
                deleted += 1
                continue
            if RevocationState.FAILED in states:
                failed += 1
            if effect_type == "telegram_channel_message":
                remaining_channel += 1
            else:
                remaining_discussion += 1
            remaining.append(
                UndoTarget(
                    effect_ids=ids,
                    peer_id=peer_id,
                    message_id=message_id,
                    effect_type=effect_type,
                )
            )

        revision = max(all_effect_ids, default=0)
        payload_hash = self._operations.payload_hash(
            self._payload(job_id, tuple(remaining))
        )
        return UndoStatus(
            job_id=job_id,
            total_messages=len(grouped),
            deleted_messages=deleted,
            failed_messages=failed,
            channel_messages=channel,
            discussion_messages=discussion,
            remaining_channel_messages=remaining_channel,
            remaining_discussion_messages=remaining_discussion,
            remaining_targets=tuple(remaining),
            expected_revision=revision,
            payload_hash=payload_hash,
        )

    @classmethod
    def _payload(cls, job_id: str, targets: tuple[UndoTarget, ...]) -> dict[str, object]:
        return {
            "version": 1,
            "action": cls.ACTION,
            "job_id": job_id,
            "targets": [
                {
                    "effect_ids": list(target.effect_ids),
                    "peer_id": target.peer_id,
                    "message_id": target.message_id,
                }
                for target in targets
            ],
        }

    @staticmethod
    def _delete_error_code(exc: Exception) -> str:
        name = type(exc).__name__.lower()
        if "flood" in name:
            return "telegram_delete_flood_wait"
        if "admin" in name or "forbidden" in name or "permission" in name:
            return "telegram_delete_permission"
        if "timeout" in name or isinstance(exc, TimeoutError):
            return "telegram_delete_timeout"
        return "telegram_delete_failed"
