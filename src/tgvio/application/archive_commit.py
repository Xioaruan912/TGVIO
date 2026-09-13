from __future__ import annotations

from tgvio.application.archive_executor_support import *  # noqa: F401,F403


class ArchiveCommitMixin:
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
