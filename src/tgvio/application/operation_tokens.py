from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable

from tgvio.application.ports import JobRepository
from tgvio.domain.operations import OperationToken


class OperationTokenInvalidError(RuntimeError):
    pass


class OperationTokenService:
    def __init__(
        self,
        repository: JobRepository,
        *,
        ttl_seconds: int = 300,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._ttl_seconds = max(30, min(900, int(ttl_seconds)))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(16))

    async def issue(
        self,
        *,
        owner_id: int,
        action: str,
        resource_type: str,
        resource_id: str,
        expected_revision: int,
        payload: dict[str, object],
    ) -> OperationToken:
        return await self._repository.create_operation_token(
            token=self._token_factory(),
            owner_id=owner_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            expected_revision=expected_revision,
            payload_hash=self.payload_hash(payload),
            payload=payload,
            ttl_seconds=self._ttl_seconds,
        )

    async def inspect(
        self,
        *,
        token: str,
        owner_id: int,
        action: str,
    ) -> OperationToken:
        operation = await self._repository.get_operation_token(token)
        if (
            operation is None
            or int(operation.owner_id) != int(owner_id)
            or operation.action != action
            or operation.consumed_at is not None
        ):
            raise OperationTokenInvalidError("operation is unavailable")
        return operation

    async def consume(
        self,
        *,
        token: str,
        owner_id: int,
        action: str,
        resource_type: str,
        resource_id: str,
        expected_revision: int,
        payload: dict[str, object],
    ) -> OperationToken:
        payload_hash = self.payload_hash(payload)
        consumed = await self._repository.consume_operation_token(
            token=token,
            owner_id=owner_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            expected_revision=expected_revision,
            payload_hash=payload_hash,
        )
        if consumed is None:
            raise OperationTokenInvalidError("operation expired, changed, or was already consumed")
        return consumed

    @staticmethod
    def payload_hash(payload: dict[str, object]) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
