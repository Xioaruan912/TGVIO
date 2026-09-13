from __future__ import annotations

from tgvio.application.archive_executor_support import *  # noqa: F401,F403


class ArchiveProbeMixin:
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
