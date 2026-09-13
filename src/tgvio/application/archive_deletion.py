from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import urllib.parse
from collections.abc import Callable

from tgvio.application.operation_tokens import (
    OperationTokenInvalidError,
    OperationTokenService,
)
from tgvio.application.ports import ArchiveTransport, JobRepository
from tgvio.application.scheduler import PhaseClaimGuard
from tgvio.domain.archive import (
    ArchiveDeletion,
    ArchiveDeletionConfirmation,
    ArchiveDeletionResult,
    ArchiveDeletionState,
    ArchiveDeletionStatus,
    ArchiveDeletionTarget,
    ArchiveDeletionTargetKind,
    ArchiveDeletionTargetState,
    ArchivePackage,
    ArchivePackageState,
    ArchiveObjectState,
    archive_complete_marker,
    archive_json_bytes,
    archive_json_sha256,
)
from tgvio.domain.job import Job
from tgvio.observability import log_event


class ArchiveDeletionUnavailableError(RuntimeError):
    pass


class ArchiveDeletionOperationInvalidError(RuntimeError):
    pass


class ArchiveDeletionSafetyError(RuntimeError):
    pass


class ArchiveDeletionService:
    """Delete only the immutable, receipt-derived files of one archive package."""

    ACTION = "archive_delete"
    RESOURCE_TYPE = "archive_package"
    _RETRYABLE_DELETE_CODES = {
        "archive_delete_timeout",
        "archive_delete_unreachable",
        "archive_delete_rate_limited",
        "archive_delete_server_error",
        "archive_delete_unverified",
    }

    def __init__(
        self,
        repository: JobRepository,
        transport: ArchiveTransport,
        *,
        operation_tokens: OperationTokenService | None = None,
        ttl_seconds: int = 300,
        token_factory: Callable[[], str] | None = None,
        delete_timeout_seconds: float = 15.0,
        delete_attempts: int = 2,
        delete_retry_delay_seconds: float = 0.5,
        max_consecutive_failures: int = 2,
        delete_batch_timeout_seconds: float = 60.0,
        claim_ttl_seconds: int = 120,
    ) -> None:
        self._repository = repository
        self._transport = transport
        self._operations = operation_tokens or OperationTokenService(
            repository,
            ttl_seconds=ttl_seconds,
            token_factory=token_factory,
        )
        self._delete_timeout_seconds = max(
            0.01,
            min(120.0, float(delete_timeout_seconds)),
        )
        self._delete_attempts = max(1, min(3, int(delete_attempts)))
        self._delete_retry_delay_seconds = max(
            0.0,
            min(5.0, float(delete_retry_delay_seconds)),
        )
        self._max_consecutive_failures = max(
            1,
            min(10, int(max_consecutive_failures)),
        )
        self._delete_batch_timeout_seconds = max(
            self._delete_timeout_seconds,
            min(300.0, float(delete_batch_timeout_seconds)),
        )
        self._claim_ttl_seconds = max(
            30,
            min(600, int(claim_ttl_seconds)),
        )
        self._log = logging.getLogger("tgvio.archive.deletion")

    async def status(self, package: ArchivePackage) -> ArchiveDeletionStatus | None:
        deletion = await self._repository.get_archive_deletion(package.id)
        if deletion is None:
            return None
        desired = self._targets(package)
        self._validate_frozen_deletion(deletion, desired)
        return self._build_status(deletion)

    async def prepare(
        self,
        job: Job,
        *,
        owner_id: int,
    ) -> ArchiveDeletionConfirmation:
        if int(job.owner_id) != int(owner_id):
            raise ArchiveDeletionUnavailableError("job owner mismatch")
        if not job.terminal:
            raise ArchiveDeletionUnavailableError("job is not terminal")
        package = await self._repository.get_archive_package_for_job(job.id)
        if package is None or package.state != ArchivePackageState.COMMITTED:
            raise ArchiveDeletionUnavailableError("archive package is not committed")
        targets = self._targets(package)
        target_set_hash = self._target_set_hash(targets)
        try:
            deletion = await self._repository.ensure_archive_deletion(
                package.id,
                target_set_hash=target_set_hash,
                targets=targets,
            )
        except (KeyError, ValueError) as exc:
            raise ArchiveDeletionSafetyError("archive deletion target set is unsafe") from exc
        self._validate_frozen_deletion(deletion, targets)
        status = self._build_status(deletion)
        if status.complete:
            raise ArchiveDeletionUnavailableError("archive package is already deleted")
        operation = await self._operations.issue(
            owner_id=owner_id,
            action=self.ACTION,
            resource_type=self.RESOURCE_TYPE,
            resource_id=package.id,
            expected_revision=status.expected_revision,
            payload=self._operation_payload(deletion, status.remaining_targets),
        )
        return ArchiveDeletionConfirmation(operation=operation, status=status)

    async def confirm(self, *, owner_id: int, token: str) -> ArchiveDeletionResult:
        try:
            operation = await self._operations.inspect(
                token=token,
                owner_id=owner_id,
                action=self.ACTION,
            )
        except OperationTokenInvalidError as exc:
            raise ArchiveDeletionOperationInvalidError("operation is unavailable") from exc
        if operation.resource_type != self.RESOURCE_TYPE:
            raise ArchiveDeletionOperationInvalidError("operation resource is invalid")

        package = await self._repository.get_archive_package(operation.resource_id)
        if package is None or package.state != ArchivePackageState.COMMITTED:
            raise ArchiveDeletionOperationInvalidError("archive package state changed")
        job = await self._repository.get(package.job_id)
        if (
            job is None
            or int(job.owner_id) != int(owner_id)
            or not job.terminal
        ):
            raise ArchiveDeletionOperationInvalidError("job state changed")

        claim = PhaseClaimGuard(
            self._repository,
            job.id,
            "archive_delete",
            ttl_seconds=self._claim_ttl_seconds,
        )
        if not await claim.start():
            raise ArchiveDeletionOperationInvalidError("archive deletion is already running")
        try:
            deletion, status = await self._validated_status(package)
            if (
                status.expected_revision != operation.expected_revision
                or status.payload_hash != operation.payload_hash
            ):
                raise ArchiveDeletionOperationInvalidError("archive deletion state changed")
            try:
                await self._operations.consume(
                    token=operation.token,
                    owner_id=owner_id,
                    action=self.ACTION,
                    resource_type=self.RESOURCE_TYPE,
                    resource_id=package.id,
                    expected_revision=status.expected_revision,
                    payload=self._operation_payload(deletion, status.remaining_targets),
                )
            except OperationTokenInvalidError as exc:
                raise ArchiveDeletionOperationInvalidError(
                    "operation expired or was already consumed"
                ) from exc
            try:
                started = await self._repository.begin_archive_deletion(
                    package.id,
                    expected_revision=status.expected_revision,
                )
            except (KeyError, ValueError) as exc:
                raise ArchiveDeletionOperationInvalidError(
                    "archive deletion state changed"
                ) from exc
            return await claim.run(self._execute(job, started))
        finally:
            await claim.stop()

    async def _validated_status(
        self,
        package: ArchivePackage,
    ) -> tuple[ArchiveDeletion, ArchiveDeletionStatus]:
        targets = self._targets(package)
        try:
            deletion = await self._repository.ensure_archive_deletion(
                package.id,
                target_set_hash=self._target_set_hash(targets),
                targets=targets,
            )
        except (KeyError, ValueError) as exc:
            raise ArchiveDeletionOperationInvalidError(
                "archive deletion target set changed"
            ) from exc
        self._validate_frozen_deletion(deletion, targets)
        return deletion, self._build_status(deletion)

    async def _execute(
        self,
        job: Job,
        deletion: ArchiveDeletion,
    ) -> ArchiveDeletionResult:
        initial = self._build_status(deletion)
        deleted_now = 0
        failed_now = 0
        consecutive_failures = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._delete_batch_timeout_seconds

        for target in sorted(
            initial.remaining_targets,
            key=lambda value: value.target_index,
        ):
            if loop.time() >= deadline:
                break
            if target.kind == ArchiveDeletionTargetKind.MANIFEST:
                current_deletion = await self._repository.get_archive_deletion(
                    deletion.package_id
                )
                if current_deletion is None:
                    raise ArchiveDeletionSafetyError("archive deletion disappeared")
                if any(
                    item.kind == ArchiveDeletionTargetKind.OBJECT
                    and item.state != ArchiveDeletionTargetState.DELETED
                    for item in current_deletion.targets
                ):
                    break
            if target.id is None:
                raise ArchiveDeletionSafetyError("durable deletion target id is missing")
            deleted = False
            attempted = False
            last_code: str | None = None
            for attempt in range(self._delete_attempts):
                remaining_seconds = deadline - loop.time()
                if remaining_seconds <= 0:
                    break
                attempted = True
                current = await self._repository.begin_archive_deletion_target(
                    deletion.package_id,
                    target.id,
                )
                try:
                    receipt = await asyncio.wait_for(
                        self._transport.delete_file(
                            current.remote_path,
                            expected_size=current.expected_size_bytes,
                            expected_etag=current.expected_etag,
                            expected_sha256=current.expected_sha256,
                        ),
                        timeout=min(self._delete_timeout_seconds, remaining_seconds),
                    )
                except Exception as exc:
                    last_code = self._delete_error_code(exc)
                    await self._repository.checkpoint_archive_deletion_target(
                        deletion.package_id,
                        target.id,
                        state=ArchiveDeletionTargetState.FAILED,
                        error_code=last_code,
                    )
                    log_event(
                        self._log,
                        logging.WARNING,
                        "archive.delete.target_failed",
                        "Exact Archive target deletion failed",
                        job_id=job.id,
                        package_id=deletion.package_id,
                        target_index=target.target_index,
                        target_kind=target.kind.value,
                        error_code=last_code,
                        exception_type=type(exc).__name__,
                    )
                    should_retry = (
                        last_code in self._RETRYABLE_DELETE_CODES
                        and attempt + 1 < self._delete_attempts
                    )
                    if not should_retry:
                        break
                    if self._delete_retry_delay_seconds:
                        delay = min(
                            self._delete_retry_delay_seconds * (2**attempt),
                            max(0.0, deadline - loop.time()),
                        )
                        if delay:
                            await asyncio.sleep(delay)
                else:
                    if receipt.remote_path != current.remote_path:
                        await self._repository.checkpoint_archive_deletion_target(
                            deletion.package_id,
                            target.id,
                            state=ArchiveDeletionTargetState.FAILED,
                            error_code="archive_delete_conflict",
                        )
                        raise ArchiveDeletionSafetyError(
                            "archive transport returned a receipt for another target"
                        )
                    await self._repository.checkpoint_archive_deletion_target(
                        deletion.package_id,
                        target.id,
                        state=ArchiveDeletionTargetState.DELETED,
                        verification_method=receipt.verification_method,
                        already_missing=receipt.already_missing,
                    )
                    log_event(
                        self._log,
                        logging.INFO,
                        "archive.delete.target_succeeded",
                        job_id=job.id,
                        package_id=deletion.package_id,
                        target_index=target.target_index,
                        target_kind=target.kind.value,
                        already_missing=receipt.already_missing,
                    )
                    deleted = True
                    break

            if deleted:
                deleted_now += 1
                consecutive_failures = 0
                continue
            if not attempted:
                break
            failed_now += 1
            consecutive_failures += 1
            if target.kind == ArchiveDeletionTargetKind.COMMIT_MARKER:
                break
            if consecutive_failures >= self._max_consecutive_failures:
                break

        final_deletion = await self._repository.finalize_archive_deletion(
            deletion.package_id
        )
        final = self._build_status(final_deletion)
        log_event(
            self._log,
            logging.INFO if final.complete else logging.WARNING,
            (
                "archive.delete.completed"
                if final.complete
                else "archive.delete.partial"
            ),
            job_id=job.id,
            package_id=deletion.package_id,
            total_targets=final.total_targets,
            deleted_targets=final.deleted_targets,
            remaining_targets=final.remaining_count,
            failed_now=failed_now,
        )
        return ArchiveDeletionResult(
            job_id=job.id,
            package_id=deletion.package_id,
            total_targets=final.total_targets,
            deleted_now=deleted_now,
            deleted_total=final.deleted_targets,
            failed_now=failed_now,
            remaining_targets=final.remaining_count,
        )

    def _targets(self, package: ArchivePackage) -> tuple[ArchiveDeletionTarget, ...]:
        if package.state != ArchivePackageState.COMMITTED:
            raise ArchiveDeletionSafetyError("archive package is not committed")
        package_parts = self._safe_parts(package.remote_path)
        if not package_parts:
            raise ArchiveDeletionSafetyError("archive package remote root is unsafe")
        manifest = archive_json_bytes(package.manifest)
        manifest_sha256 = archive_json_sha256(package.manifest)
        if package.manifest_sha256 and package.manifest_sha256 != manifest_sha256:
            raise ArchiveDeletionSafetyError("archive manifest receipt changed")
        marker = archive_json_bytes(archive_complete_marker(package))

        targets: list[ArchiveDeletionTarget] = [
            ArchiveDeletionTarget(
                package_id=package.id,
                target_index=0,
                kind=ArchiveDeletionTargetKind.COMMIT_MARKER,
                remote_path=self._exact_child(package.remote_path, "_COMPLETE.json"),
                expected_size_bytes=len(marker),
                expected_sha256=hashlib.sha256(marker).hexdigest(),
            )
        ]
        for obj in sorted(package.objects, key=lambda value: value.object_index):
            if obj.id is None or obj.state != ArchiveObjectState.STORED:
                raise ArchiveDeletionSafetyError(
                    "committed archive has no stored object receipt"
                )
            object_parts = self._safe_parts(obj.remote_relpath)
            if len(object_parts) < 2 or object_parts[0] != "media":
                raise ArchiveDeletionSafetyError("archive object path is outside media")
            remote_path = self._exact_child(package.remote_path, obj.remote_relpath)
            if self._safe_parts(remote_path)[: len(package_parts)] != package_parts:
                raise ArchiveDeletionSafetyError("archive object escaped its package")
            targets.append(
                ArchiveDeletionTarget(
                    package_id=package.id,
                    target_index=len(targets),
                    kind=ArchiveDeletionTargetKind.OBJECT,
                    archive_object_id=obj.id,
                    remote_path=remote_path,
                    expected_size_bytes=obj.size_bytes,
                    expected_etag=obj.remote_etag,
                )
            )
        targets.append(
            ArchiveDeletionTarget(
                package_id=package.id,
                target_index=len(targets),
                kind=ArchiveDeletionTargetKind.MANIFEST,
                remote_path=self._exact_child(package.remote_path, "manifest.json"),
                expected_size_bytes=len(manifest),
                expected_sha256=manifest_sha256,
            )
        )
        remote_paths = {target.remote_path for target in targets}
        if len(remote_paths) != len(targets):
            raise ArchiveDeletionSafetyError("archive deletion target paths overlap")
        return tuple(targets)

    def _build_status(self, deletion: ArchiveDeletion) -> ArchiveDeletionStatus:
        self._validate_target_set_hash(deletion)
        remaining = tuple(
            target
            for target in sorted(deletion.targets, key=lambda value: value.target_index)
            if target.state != ArchiveDeletionTargetState.DELETED
        )
        payload = self._operation_payload(deletion, remaining)
        return ArchiveDeletionStatus(
            package_id=deletion.package_id,
            state=deletion.state,
            total_targets=len(deletion.targets),
            deleted_targets=sum(
                1
                for target in deletion.targets
                if target.state == ArchiveDeletionTargetState.DELETED
            ),
            failed_targets=sum(
                1
                for target in deletion.targets
                if target.state == ArchiveDeletionTargetState.FAILED
            ),
            remaining_targets=remaining,
            expected_revision=deletion.revision,
            payload_hash=self._operations.payload_hash(payload),
            commit_boundary_invalidated=any(
                target.kind == ArchiveDeletionTargetKind.COMMIT_MARKER
                and target.state == ArchiveDeletionTargetState.DELETED
                for target in deletion.targets
            ),
        )

    def _validate_frozen_deletion(
        self,
        deletion: ArchiveDeletion,
        desired: tuple[ArchiveDeletionTarget, ...],
    ) -> None:
        self._validate_target_set_hash(deletion)
        if deletion.target_set_hash != self._target_set_hash(desired):
            raise ArchiveDeletionSafetyError("archive deletion target set changed")
        if tuple(self._target_identity(target) for target in deletion.targets) != tuple(
            self._target_identity(target) for target in desired
        ):
            raise ArchiveDeletionSafetyError("archive deletion target set changed")

    def _validate_target_set_hash(self, deletion: ArchiveDeletion) -> None:
        if deletion.target_set_hash != self._target_set_hash(deletion.targets):
            raise ArchiveDeletionSafetyError("archive deletion target-set hash mismatch")

    def _target_set_hash(self, targets: tuple[ArchiveDeletionTarget, ...]) -> str:
        return self._operations.payload_hash(
            {
                "version": 1,
                "targets": [self._target_payload(target, include_id=False) for target in targets],
            }
        )

    def _operation_payload(
        self,
        deletion: ArchiveDeletion,
        targets: tuple[ArchiveDeletionTarget, ...],
    ) -> dict[str, object]:
        return {
            "version": 1,
            "action": self.ACTION,
            "package_id": deletion.package_id,
            "target_set_hash": deletion.target_set_hash,
            "targets": [self._target_payload(target, include_id=True) for target in targets],
        }

    @staticmethod
    def _target_payload(
        target: ArchiveDeletionTarget,
        *,
        include_id: bool,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "target_index": target.target_index,
            "kind": target.kind.value,
            "archive_object_id": target.archive_object_id,
            "remote_path_sha256": hashlib.sha256(
                target.remote_path.encode("utf-8")
            ).hexdigest(),
            "expected_size_bytes": target.expected_size_bytes,
            "expected_sha256": target.expected_sha256 or "",
            "expected_etag": target.expected_etag or "",
        }
        if include_id:
            payload["target_id"] = target.id
        return payload

    @staticmethod
    def _target_identity(target: ArchiveDeletionTarget) -> tuple[object, ...]:
        return (
            target.target_index,
            target.kind.value,
            target.archive_object_id,
            target.remote_path,
            target.expected_size_bytes,
            target.expected_sha256,
            target.expected_etag,
        )

    @classmethod
    def _exact_child(cls, package_path: str, child_path: str) -> str:
        package_parts = cls._safe_parts(package_path)
        child_parts = cls._safe_parts(child_path)
        if not package_parts or not child_parts:
            raise ArchiveDeletionSafetyError("archive deletion path is empty")
        combined = f"{package_path}/{child_path}"
        combined_parts = cls._safe_parts(combined)
        if combined_parts[: len(package_parts)] != package_parts:
            raise ArchiveDeletionSafetyError("archive deletion path escaped its package")
        return combined

    @staticmethod
    def _safe_parts(value: str) -> tuple[str, ...]:
        raw = str(value or "").strip()
        decoded = urllib.parse.unquote(raw)
        if (
            not decoded
            or decoded.startswith("/")
            or decoded.endswith("/")
            or "\\" in decoded
            or "\x00" in decoded
        ):
            raise ArchiveDeletionSafetyError("unsafe archive deletion path")
        parts = tuple(decoded.split("/"))
        if any(
            not part
            or part in {".", ".."}
            or re.search(r"[\x00-\x1f\x7f]", part)
            for part in parts
        ):
            raise ArchiveDeletionSafetyError("unsafe archive deletion path")
        return parts

    @staticmethod
    def _delete_error_code(exc: Exception) -> str:
        name = type(exc).__name__.lower()
        status = getattr(exc, "status", None)
        if "safety" in name or status in {409, 412}:
            return "archive_delete_conflict"
        if status in {401, 403} or any(
            marker in name for marker in ("permission", "forbidden", "unauthorized")
        ):
            return "archive_delete_permission"
        if status == 429 or "rate" in name or "flood" in name:
            return "archive_delete_rate_limited"
        if isinstance(status, int) and status >= 500:
            return "archive_delete_server_error"
        if isinstance(exc, TimeoutError) or "timeout" in name:
            return "archive_delete_timeout"
        if any(marker in name for marker in ("connection", "network", "socket", "dns")):
            return "archive_delete_unreachable"
        if "verif" in name or "webdavarchiveerror" in name:
            return "archive_delete_unverified"
        return "archive_delete_failed"
