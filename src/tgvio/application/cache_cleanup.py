from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import shutil

from tgvio.application.ports import JobRepository
from tgvio.domain.archive import ArchivePackageState
from tgvio.domain.job import Job, JobState


_ARCHIVE_BLOCKING = {
    ArchivePackageState.PLANNED,
    ArchivePackageState.STAGING,
    ArchivePackageState.UPLOADING,
    ArchivePackageState.VERIFYING,
    ArchivePackageState.FAILED,
}
_AUTO_CLEAN_STATES = (JobState.SUCCEEDED, JobState.CANCELLED)


@dataclass(frozen=True, slots=True)
class CacheStats:
    bytes_used: int
    managed_dirs: int
    eligible_jobs: int
    blocked_by_archive: int
    retention_hours: int


@dataclass(frozen=True, slots=True)
class CacheCleanupResult:
    removed_jobs: int
    removed_bytes: int
    blocked_by_archive: int


class CacheCleanupService:
    """Safely remove managed local cache only after a job is terminal."""

    def __init__(
        self,
        repository: JobRepository,
        download_root: Path,
        *,
        retention_hours: int,
    ) -> None:
        self._repository = repository
        self._download_root = download_root.resolve()
        self._retention_hours = max(1, int(retention_hours))

    async def stats(self) -> CacheStats:
        jobs = await self._repository.list_by_states(_AUTO_CLEAN_STATES)
        eligible = 0
        blocked = 0
        for job in jobs:
            if not self._age_due(job):
                continue
            if await self._archive_blocks(job):
                blocked += 1
            else:
                eligible += 1
        bytes_used, managed_dirs = await asyncio.to_thread(self._disk_usage)
        return CacheStats(
            bytes_used=bytes_used,
            managed_dirs=managed_dirs,
            eligible_jobs=eligible,
            blocked_by_archive=blocked,
            retention_hours=self._retention_hours,
        )

    async def cleanup(self, *, force: bool = False) -> CacheCleanupResult:
        jobs = await self._repository.list_by_states(_AUTO_CLEAN_STATES)
        removed_jobs = 0
        removed_bytes = 0
        blocked = 0
        for job in jobs:
            if not force and not self._age_due(job):
                continue
            if await self._archive_blocks(job):
                blocked += 1
                continue
            path = self._job_dir(job.id)
            size = await asyncio.to_thread(self._path_size, path)
            await asyncio.to_thread(self._remove_tree, path)
            cleaned_items = []
            for item in job.items:
                metadata = dict(item.metadata)
                metadata["cache_cleaned"] = True
                cleaned_items.append(replace(item, local_path=None, metadata=metadata))
            job.items = cleaned_items
            await self._repository.save(job)
            removed_jobs += 1
            removed_bytes += size
        return CacheCleanupResult(
            removed_jobs=removed_jobs,
            removed_bytes=removed_bytes,
            blocked_by_archive=blocked,
        )

    async def _archive_blocks(self, job: Job) -> bool:
        package = await self._repository.get_archive_package_for_job(job.id)
        return bool(package and package.state in _ARCHIVE_BLOCKING)

    def _age_due(self, job: Job) -> bool:
        value = job.updated_at or job.created_at
        if not value:
            return False
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0
        return age_hours >= self._retention_hours

    def _job_dir(self, job_id: str) -> Path:
        path = (self._download_root / f"job-{job_id}").resolve()
        if path.parent != self._download_root:
            raise ValueError("managed cache path escaped download root")
        return path

    def _disk_usage(self) -> tuple[int, int]:
        if not self._download_root.is_dir():
            return 0, 0
        total = 0
        count = 0
        for path in self._download_root.glob("job-*"):
            if not path.is_dir():
                continue
            count += 1
            total += self._path_size(path)
        return total, count

    @staticmethod
    def _path_size(path: Path) -> int:
        if not path.is_dir():
            return 0
        total = 0
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except OSError:
                continue
        return total

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


class CacheCleanupRuntime:
    def __init__(
        self,
        service: CacheCleanupService,
        *,
        interval_minutes: int,
    ) -> None:
        self._service = service
        self._interval_seconds = max(60, int(interval_minutes) * 60)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="tgvio-cache-cleanup")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def stats(self) -> CacheStats:
        return await self._service.stats()

    async def cleanup(self, *, force: bool = False) -> CacheCleanupResult:
        return await self._service.cleanup(force=force)

    async def _run(self) -> None:
        while True:
            try:
                await self._service.cleanup(force=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self._interval_seconds)
