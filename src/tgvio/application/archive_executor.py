from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
import time

from tgvio.application.archive_capabilities import (
    record_archive_probe_failure,
    record_archive_probe_success,
)
from tgvio.application.ports import ArchiveTransport, JobRepository
from tgvio.domain.archive import (
    ArchiveCapabilities,
    ArchiveObject,
    ArchiveObjectState,
    ArchivePackage,
    ArchivePackageState,
    archive_complete_marker,
    archive_json_bytes,
    archive_json_sha256,
)
from tgvio.observability import log_event


class ArchiveCapabilityError(RuntimeError):
    pass


class ArchiveExecutionError(RuntimeError):
    pass


class ArchiveExecutor:
    """Durably materialize one ArchivePackage and commit it exactly once."""

    def __init__(self, repository: JobRepository, transport: ArchiveTransport) -> None:
        self._repository = repository
        self._transport = transport
        self._log = logging.getLogger("tgvio.archive.execution")

    async def execute(self, package: ArchivePackage) -> ArchivePackage:
        started_at = time.monotonic()
        current = await self._repository.get_archive_package(package.id)
        if current is None:
            raise KeyError(f"archive package not found: {package.id}")
        if current.state == ArchivePackageState.COMMITTED:
            return current
        if current.state == ArchivePackageState.CANCELLED:
            raise ArchiveExecutionError("cancelled archive package cannot execute")

        try:
            capabilities = await self._transport.probe()
        except Exception as exc:
            await self._fail_package(
                current,
                code="archive_probe_failed",
                exc=exc,
            )
            await self._record_probe_failure(current)
            raise
        await self._record_probe_success(current, capabilities)
        log_event(
            self._log,
            logging.INFO,
            "archive.execution.started",
            package_id=current.id,
            job_id=current.job_id,
            archive_state=current.state.value,
            object_count=len(current.objects),
            total_bytes=sum(obj.size_bytes for obj in current.objects),
            commit_mode=capabilities.commit_mode,
        )
        try:
            self._validate_capabilities(capabilities)
        except ArchiveCapabilityError as exc:
            await self._fail_package(
                current,
                code="archive_capability_missing",
                exc=exc,
            )
            raise

        try:
            if await self._committed_remote_is_valid(current, capabilities):
                current = await self._advance_to_verifying(
                    current,
                    capabilities,
                    event_prefix="archive_commit_recovered",
                )
                committed = await self._repository.update_archive_package_state(
                    current.id,
                    ArchivePackageState.COMMITTED,
                    event_type="archive_committed_recovered",
                    detail={"commit_mode": capabilities.commit_mode},
                    committed=True,
                )
                log_event(
                    self._log,
                    logging.INFO,
                    "archive.execution.recovered",
                    package_id=current.id,
                    job_id=current.job_id,
                    commit_mode=capabilities.commit_mode,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
                return committed

            if current.state == ArchivePackageState.VERIFYING:
                await self._verify_final_package(current, capabilities)
                await self._write_complete_marker(current, capabilities)
                committed = await self._repository.update_archive_package_state(
                    current.id,
                    ArchivePackageState.COMMITTED,
                    event_type="archive_committed",
                    detail={"commit_mode": capabilities.commit_mode},
                    committed=True,
                )
                log_event(
                    self._log,
                    logging.INFO,
                    "archive.execution.completed",
                    package_id=current.id,
                    job_id=current.job_id,
                    commit_mode=capabilities.commit_mode,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
                return committed

            current = await self._enter_uploading(current, capabilities)
            work_root = await self._work_root(current, capabilities)
            await self._transport.ensure_collection(f"{work_root}/media")
            for obj in current.objects:
                await self._store_object(current, obj, work_root)

            current = await self._repository.update_archive_package_state(
                current.id,
                ArchivePackageState.VERIFYING,
                event_type="archive_verification_started",
                detail={"commit_mode": capabilities.commit_mode},
            )
            await self._write_manifest(current, work_root, capabilities)
            if capabilities.supports_move and work_root == current.staging_path:
                await self._transport.move_collection(current.staging_path, current.remote_path)
            await self._verify_final_package(current, capabilities)
            await self._write_complete_marker(current, capabilities)
            committed = await self._repository.update_archive_package_state(
                current.id,
                ArchivePackageState.COMMITTED,
                event_type="archive_committed",
                detail={"commit_mode": capabilities.commit_mode},
                committed=True,
            )
            log_event(
                self._log,
                logging.INFO,
                "archive.execution.completed",
                package_id=current.id,
                job_id=current.job_id,
                commit_mode=capabilities.commit_mode,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            return committed
        except Exception as exc:
            refreshed = await self._repository.get_archive_package(current.id)
            if refreshed is not None and refreshed.state not in {
                ArchivePackageState.COMMITTED,
                ArchivePackageState.CANCELLED,
                ArchivePackageState.FAILED,
            }:
                await self._fail_package(
                    refreshed,
                    code="archive_execution_failed",
                    exc=exc,
                )
            log_event(
                self._log,
                logging.ERROR,
                "archive.execution.failed",
                "Archive execution failed",
                package_id=current.id,
                job_id=current.job_id,
                exception_type=type(exc).__name__,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                exc_info=True,
            )
            raise

    @staticmethod
    def _validate_capabilities(capabilities: ArchiveCapabilities) -> None:
        missing: list[str] = []
        if not capabilities.supports_propfind:
            missing.append("PROPFIND")
        if not capabilities.supports_mkcol:
            missing.append("MKCOL")
        if not capabilities.supports_put:
            missing.append("PUT")
        if missing:
            raise ArchiveCapabilityError(
                "required WebDAV methods unavailable: " + ",".join(missing)
            )

    async def _enter_uploading(
        self,
        package: ArchivePackage,
        capabilities: ArchiveCapabilities,
    ) -> ArchivePackage:
        current = package
        if current.state in {ArchivePackageState.PLANNED, ArchivePackageState.FAILED}:
            current = await self._repository.update_archive_package_state(
                current.id,
                ArchivePackageState.STAGING,
                event_type=(
                    "archive_staging_started"
                    if package.state == ArchivePackageState.PLANNED
                    else "archive_resume_started"
                ),
                detail={"commit_mode": capabilities.commit_mode},
            )
        if current.state == ArchivePackageState.STAGING:
            current = await self._repository.update_archive_package_state(
                current.id,
                ArchivePackageState.UPLOADING,
                event_type="archive_upload_started",
                detail={"commit_mode": capabilities.commit_mode},
            )
        if current.state != ArchivePackageState.UPLOADING:
            raise ArchiveExecutionError(
                f"archive package cannot upload from {current.state.value}"
            )
        return current

    async def _advance_to_verifying(
        self,
        package: ArchivePackage,
        capabilities: ArchiveCapabilities,
        *,
        event_prefix: str,
    ) -> ArchivePackage:
        current = package
        if current.state in {ArchivePackageState.PLANNED, ArchivePackageState.FAILED}:
            current = await self._repository.update_archive_package_state(
                current.id,
                ArchivePackageState.STAGING,
                event_type=f"{event_prefix}_staging",
                detail={"commit_mode": capabilities.commit_mode},
            )
        if current.state == ArchivePackageState.STAGING:
            current = await self._repository.update_archive_package_state(
                current.id,
                ArchivePackageState.UPLOADING,
                event_type=f"{event_prefix}_uploading",
                detail={"commit_mode": capabilities.commit_mode},
            )
        if current.state == ArchivePackageState.UPLOADING:
            current = await self._repository.update_archive_package_state(
                current.id,
                ArchivePackageState.VERIFYING,
                event_type=f"{event_prefix}_verifying",
                detail={"commit_mode": capabilities.commit_mode},
            )
        if current.state != ArchivePackageState.VERIFYING:
            raise ArchiveExecutionError(
                f"archive package cannot commit from {current.state.value}"
            )
        return current

    async def _work_root(
        self,
        package: ArchivePackage,
        capabilities: ArchiveCapabilities,
    ) -> str:
        if not capabilities.supports_move:
            return package.remote_path
        final_manifest = await self._transport.stat(f"{package.remote_path}/manifest.json")
        return package.remote_path if final_manifest.exists else package.staging_path

    async def _store_object(
        self,
        package: ArchivePackage,
        obj: ArchiveObject,
        work_root: str,
    ) -> None:
        if obj.id is None:
            raise ArchiveExecutionError("durable archive object id is missing")
        remote_path = f"{work_root}/{obj.remote_relpath}"
        remote = await self._transport.stat(remote_path)
        resumable_states = {
            ArchiveObjectState.UPLOADING,
            ArchiveObjectState.VERIFYING,
            ArchiveObjectState.STORED,
            ArchiveObjectState.FAILED,
        }
        if (
            obj.state in resumable_states
            and remote.exists
            and remote.size_bytes == obj.size_bytes
        ):
            if obj.state != ArchiveObjectState.STORED:
                current = obj
                if current.state != ArchiveObjectState.UPLOADING:
                    current = await self._repository.update_archive_object_state(
                        obj.id,
                        ArchiveObjectState.UPLOADING,
                        event_type="archive_object_reconciled",
                        detail={"object_index": obj.object_index},
                    )
                await self._repository.update_archive_object_state(
                    current.id or obj.id,
                    ArchiveObjectState.STORED,
                    event_type="archive_object_stored",
                    verification_method="size",
                    remote_etag=remote.etag,
                    error_code=None,
                    error_message=None,
                    detail={
                        "object_index": obj.object_index,
                        "reused_remote": True,
                        "verification_method": "size",
                    },
                )
            log_event(
                self._log,
                logging.INFO,
                "archive.object.reused",
                package_id=package.id,
                job_id=package.job_id,
                object_index=obj.object_index,
                bytes_done=obj.size_bytes,
            )
            return

        local = Path(obj.local_path)
        current = obj
        if current.state != ArchiveObjectState.UPLOADING:
            current = await self._repository.update_archive_object_state(
                obj.id,
                ArchiveObjectState.UPLOADING,
                event_type="archive_object_upload_started",
                error_code=None,
                error_message=None,
                detail={"object_index": obj.object_index},
            )
        try:
            await self._verify_local_object(local, obj)
            receipt = await self._transport.put_file(
                local,
                remote_path,
                obj.size_bytes,
            )
        except Exception as exc:
            await self._repository.update_archive_object_state(
                current.id or obj.id,
                ArchiveObjectState.FAILED,
                event_type="archive_object_failed",
                retry_count=obj.retry_count + 1,
                error_code="archive_object_transfer_failed",
                error_message=f"{type(exc).__name__}: archive object transfer failed",
                detail={"object_index": obj.object_index},
            )
            log_event(
                self._log,
                logging.ERROR,
                "archive.object.failed",
                "Archive object transfer failed",
                package_id=package.id,
                job_id=package.job_id,
                object_index=obj.object_index,
                error_code="archive_object_transfer_failed",
                exception_type=type(exc).__name__,
            )
            raise
        await self._repository.update_archive_object_state(
            current.id or obj.id,
            ArchiveObjectState.STORED,
            event_type="archive_object_stored",
            verification_method=receipt.verification_method,
            remote_etag=receipt.etag,
            error_code=None,
            error_message=None,
            detail={
                "object_index": obj.object_index,
                "reused_remote": receipt.reused_remote,
                "verification_method": receipt.verification_method,
            },
        )
        log_event(
            self._log,
            logging.INFO,
            "archive.object.stored",
            package_id=package.id,
            job_id=package.job_id,
            object_index=obj.object_index,
            bytes_done=obj.size_bytes,
            verification_method=receipt.verification_method,
            reused_remote=receipt.reused_remote,
        )

    async def _write_manifest(
        self,
        package: ArchivePackage,
        work_root: str,
        capabilities: ArchiveCapabilities,
    ) -> None:
        payload = archive_json_bytes(package.manifest)
        expected_hash = package.manifest_sha256 or archive_json_sha256(package.manifest)
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ArchiveExecutionError("archive manifest hash changed after planning")
        await self._transport.put_bytes(
            payload,
            f"{work_root}/manifest.json",
            content_type="application/json; charset=utf-8",
        )
        await self._verify_exact_metadata(
            f"{work_root}/manifest.json",
            payload,
            capabilities,
        )

    async def _write_complete_marker(
        self,
        package: ArchivePackage,
        capabilities: ArchiveCapabilities,
    ) -> None:
        payload = archive_json_bytes(archive_complete_marker(package))
        path = f"{package.remote_path}/_COMPLETE.json"
        await self._transport.put_bytes(
            payload,
            path,
            content_type="application/json; charset=utf-8",
        )
        await self._verify_exact_metadata(path, payload, capabilities)

    async def _verify_final_package(
        self,
        package: ArchivePackage,
        capabilities: ArchiveCapabilities,
    ) -> None:
        for obj in package.objects:
            stat = await self._transport.stat(
                f"{package.remote_path}/{obj.remote_relpath}"
            )
            if not stat.exists or stat.size_bytes != obj.size_bytes:
                raise ArchiveExecutionError(
                    f"archive final object verification failed at index {obj.object_index}"
                )
        manifest = archive_json_bytes(package.manifest)
        await self._verify_exact_metadata(
            f"{package.remote_path}/manifest.json",
            manifest,
            capabilities,
        )

    async def _committed_remote_is_valid(
        self,
        package: ArchivePackage,
        capabilities: ArchiveCapabilities,
    ) -> bool:
        marker = archive_json_bytes(archive_complete_marker(package))
        remote = await self._transport.get_bytes(
            f"{package.remote_path}/_COMPLETE.json",
            max_bytes=max(4096, len(marker) * 2),
        )
        if remote is None:
            return False
        if remote != marker:
            raise ArchiveExecutionError("archive commit marker conflicts with durable package")
        await self._verify_final_package(package, capabilities)
        return True

    async def _verify_exact_metadata(
        self,
        remote_path: str,
        payload: bytes,
        capabilities: ArchiveCapabilities,
    ) -> None:
        stat = await self._transport.stat(remote_path)
        if not stat.exists or stat.size_bytes != len(payload):
            raise ArchiveExecutionError("archive metadata size verification failed")
        if capabilities.supports_get:
            remote = await self._transport.get_bytes(
                remote_path,
                max_bytes=max(4096, len(payload) * 2),
            )
            if remote != payload:
                raise ArchiveExecutionError("archive metadata content verification failed")

    async def _record_probe_success(
        self,
        package: ArchivePackage,
        capabilities: ArchiveCapabilities,
    ) -> None:
        try:
            await record_archive_probe_success(
                self._repository,
                profile_id=package.archive_profile_id,
                capabilities=capabilities,
            )
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "archive.capability.persist_failed",
                "Archive capability snapshot could not be persisted",
                package_id=package.id,
                job_id=package.job_id,
                exception_type=type(exc).__name__,
            )

    async def _record_probe_failure(self, package: ArchivePackage) -> None:
        try:
            await record_archive_probe_failure(
                self._repository,
                profile_id=package.archive_profile_id,
                error_code="probe_failed",
            )
        except Exception as exc:
            log_event(
                self._log,
                logging.WARNING,
                "archive.probe_status.persist_failed",
                "Archive probe failure status could not be persisted",
                package_id=package.id,
                job_id=package.job_id,
                exception_type=type(exc).__name__,
            )

    async def _fail_package(
        self,
        package: ArchivePackage,
        *,
        code: str,
        exc: Exception,
    ) -> ArchivePackage:
        return await self._repository.update_archive_package_state(
            package.id,
            ArchivePackageState.FAILED,
            event_type="archive_failed",
            error_code=code,
            error_message=f"{type(exc).__name__}: archive execution failed",
            detail={"error_code": code},
        )

    @staticmethod
    async def _verify_local_object(local: Path, obj: ArchiveObject) -> None:
        if not local.is_file():
            raise FileNotFoundError("canonical archive object is missing")
        if local.stat().st_size != obj.size_bytes:
            raise ArchiveExecutionError("canonical archive object size changed")

        def digest() -> str:
            hasher = hashlib.sha256()
            with local.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    hasher.update(chunk)
            return hasher.hexdigest()

        if await asyncio.to_thread(digest) != obj.sha256:
            raise ArchiveExecutionError("canonical archive object hash changed")
