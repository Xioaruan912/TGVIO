from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.archive_deletion import (
    ArchiveDeletionOperationInvalidError,
    ArchiveDeletionSafetyError,
    ArchiveDeletionService,
)
from tgvio.application.archive_planner import ArchivePlanner
from tgvio.domain.archive import (
    ArchiveDeleteReceipt,
    ArchiveDeletionState,
    ArchiveDeletionTargetKind,
    ArchiveDeletionTargetState,
    ArchiveObjectState,
    ArchivePackageState,
)
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class FakeDeleteTransport:
    def __init__(self) -> None:
        self.files: dict[str, tuple[int, str | None]] = {}
        self.calls: list[str] = []
        self.failures: dict[str, int] = {}
        self.hanging: set[str] = set()

    async def delete_file(
        self,
        remote_path: str,
        *,
        expected_size: int,
        expected_etag: str | None = None,
        expected_sha256: str | None = None,
    ) -> ArchiveDeleteReceipt:
        self.calls.append(remote_path)
        if remote_path in self.hanging:
            await asyncio.Event().wait()
        failures = self.failures.get(remote_path, 0)
        if failures:
            self.failures[remote_path] = failures - 1
            raise TimeoutError("fixture delete timeout")
        current = self.files.get(remote_path)
        if current is None:
            return ArchiveDeleteReceipt(
                remote_path=remote_path,
                verification_method="already_absent",
                already_missing=True,
            )
        size, etag = current
        if size != expected_size or (expected_etag is not None and etag != expected_etag):
            raise RuntimeError("fixture receipt conflict")
        del self.files[remote_path]
        return ArchiveDeleteReceipt(
            remote_path=remote_path,
            verification_method="absent_after_delete",
        )


class ArchiveDeletionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = self.root / "state.sqlite3"
        self.repo = SQLiteJobRepository(self.database)
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _committed_package(self, *, item_count: int = 1):
        items = []
        for index in range(item_count):
            path = self.root / f"media-{index}.bin"
            payload = f"payload-{index}".encode()
            path.write_bytes(payload)
            items.append(
                MediaItem(
                    index=index,
                    kind=MediaKind.DOCUMENT,
                    source="fixture",
                    local_path=str(path),
                    name=path.name,
                    size_bytes=len(payload),
                    sha256=(f"{index + 1:x}" * 64)[:64],
                )
            )
        job = Job(
            id="a" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.SUCCEEDED,
            created_at="2026-09-13 01:02:03",
            items=items,
        )
        await self.repo.create(job)
        package = await self.repo.save_archive_plan(ArchivePlanner().plan(job))
        package = await self.repo.update_archive_package_state(
            package.id,
            ArchivePackageState.STAGING,
            event_type="archive_staging_started",
        )
        package = await self.repo.update_archive_package_state(
            package.id,
            ArchivePackageState.UPLOADING,
            event_type="archive_upload_started",
        )
        for obj in package.objects:
            assert obj.id is not None
            await self.repo.update_archive_object_state(
                obj.id,
                ArchiveObjectState.UPLOADING,
                event_type="archive_object_upload_started",
            )
            await self.repo.update_archive_object_state(
                obj.id,
                ArchiveObjectState.STORED,
                event_type="archive_object_stored",
                verification_method="size",
                remote_etag=f'"etag-{obj.object_index}"',
            )
        package = await self.repo.update_archive_package_state(
            package.id,
            ArchivePackageState.VERIFYING,
            event_type="archive_verification_started",
        )
        package = await self.repo.update_archive_package_state(
            package.id,
            ArchivePackageState.COMMITTED,
            event_type="archive_committed",
            committed=True,
        )
        return job, package

    @staticmethod
    def _prime(transport: FakeDeleteTransport, deletion) -> None:
        for target in deletion.targets:
            transport.files[target.remote_path] = (
                target.expected_size_bytes,
                target.expected_etag or f'"metadata-{target.target_index}"',
            )

    async def test_prepare_freezes_marker_objects_manifest_and_hides_paths_from_token(self) -> None:
        job, package = await self._committed_package(item_count=2)
        transport = FakeDeleteTransport()
        tokens = iter(("archive-delete-a", "archive-delete-b"))
        service = ArchiveDeletionService(
            self.repo,
            transport,  # type: ignore[arg-type]
            token_factory=lambda: next(tokens),
        )

        confirmation = await service.prepare(job, owner_id=42)
        deletion = await self.repo.get_archive_deletion(package.id)
        assert deletion is not None
        self.assertEqual(
            [target.kind for target in deletion.targets],
            [
                ArchiveDeletionTargetKind.COMMIT_MARKER,
                ArchiveDeletionTargetKind.OBJECT,
                ArchiveDeletionTargetKind.OBJECT,
                ArchiveDeletionTargetKind.MANIFEST,
            ],
        )
        self.assertEqual(
            [target.target_index for target in deletion.targets],
            list(range(4)),
        )
        self.assertEqual(deletion.state, ArchiveDeletionState.PREPARED)
        self.assertEqual(len(deletion.target_set_hash), 64)
        self.assertEqual(confirmation.status.remaining_count, 4)
        serialized_payload = str(confirmation.operation.payload)
        self.assertNotIn(package.remote_path, serialized_payload)
        self.assertNotIn("media/", serialized_payload)
        self.assertIn("remote_path_sha256", serialized_payload)

        await service.prepare(job, owner_id=42)
        events = await self.repo.list_archive_deletion_events(package.id)
        self.assertEqual([event.event_type for event in events], ["deletion_prepared"])
        self.assertEqual(len((await self.repo.get_archive_deletion(package.id)).targets), 4)  # type: ignore[union-attr]
        self.assertEqual(transport.calls, [])

    async def test_confirm_deletes_only_exact_files_and_preserves_all_local_facts(self) -> None:
        job, package = await self._committed_package(item_count=2)
        transport = FakeDeleteTransport()
        service = ArchiveDeletionService(
            self.repo,
            transport,  # type: ignore[arg-type]
            token_factory=lambda: "archive-delete-once",
            delete_retry_delay_seconds=0,
        )
        confirmation = await service.prepare(job, owner_id=42)
        deletion = await self.repo.get_archive_deletion(package.id)
        assert deletion is not None
        self._prime(transport, deletion)

        result = await service.confirm(owner_id=42, token=confirmation.operation.token)

        self.assertTrue(result.complete)
        self.assertEqual(transport.calls, [target.remote_path for target in deletion.targets])
        self.assertTrue(all(path != package.remote_path for path in transport.calls))
        self.assertTrue(all(not path.endswith("/") for path in transport.calls))
        self.assertEqual(transport.files, {})
        current_job = await self.repo.get(job.id)
        current_package = await self.repo.get_archive_package(package.id)
        self.assertEqual(current_job.state, JobState.SUCCEEDED)  # type: ignore[union-attr]
        self.assertEqual(current_package.state, ArchivePackageState.COMMITTED)  # type: ignore[union-attr]
        final = await self.repo.get_archive_deletion(package.id)
        assert final is not None
        self.assertEqual(final.state, ArchiveDeletionState.DELETED)
        self.assertTrue(all(target.state == ArchiveDeletionTargetState.DELETED for target in final.targets))
        events = await self.repo.list_archive_deletion_events(package.id)
        self.assertEqual(
            [event.event_type for event in events],
            [
                "deletion_prepared",
                "deletion_started",
                "target_delete_started",
                "target_delete_succeeded",
                "target_delete_started",
                "target_delete_succeeded",
                "target_delete_started",
                "target_delete_succeeded",
                "target_delete_started",
                "target_delete_succeeded",
                "deletion_completed",
            ],
        )
        with self.assertRaises(ArchiveDeletionOperationInvalidError):
            await service.confirm(owner_id=42, token=confirmation.operation.token)
        self.assertEqual(len(transport.calls), 4)

    async def test_commit_marker_failure_stops_before_any_content_and_can_resume(self) -> None:
        job, package = await self._committed_package(item_count=1)
        transport = FakeDeleteTransport()
        tokens = iter(("marker-fail", "marker-resume"))
        service = ArchiveDeletionService(
            self.repo,
            transport,  # type: ignore[arg-type]
            token_factory=lambda: next(tokens),
            delete_attempts=1,
        )
        first = await service.prepare(job, owner_id=42)
        deletion = await self.repo.get_archive_deletion(package.id)
        assert deletion is not None
        self._prime(transport, deletion)
        marker = deletion.targets[0]
        transport.failures[marker.remote_path] = 1

        partial = await service.confirm(owner_id=42, token=first.operation.token)
        self.assertFalse(partial.complete)
        self.assertEqual(transport.calls, [marker.remote_path])
        status = await service.status(package)
        assert status is not None
        self.assertFalse(status.commit_boundary_invalidated)
        self.assertEqual(status.remaining_count, 3)

        second = await service.prepare(job, owner_id=42)
        complete = await service.confirm(owner_id=42, token=second.operation.token)
        self.assertTrue(complete.complete)
        self.assertEqual(transport.calls.count(marker.remote_path), 2)
        for target in deletion.targets[1:]:
            self.assertEqual(transport.calls.count(target.remote_path), 1)

    async def test_partial_object_failure_keeps_manifest_and_restart_resumes_only_remaining(self) -> None:
        job, package = await self._committed_package(item_count=2)
        transport = FakeDeleteTransport()
        tokens = iter(("object-fail", "object-resume"))
        service = ArchiveDeletionService(
            self.repo,
            transport,  # type: ignore[arg-type]
            token_factory=lambda: next(tokens),
            delete_attempts=1,
        )
        first = await service.prepare(job, owner_id=42)
        deletion = await self.repo.get_archive_deletion(package.id)
        assert deletion is not None
        self._prime(transport, deletion)
        failed_object = deletion.targets[1]
        stored_object = deletion.targets[2]
        manifest = deletion.targets[3]
        transport.failures[failed_object.remote_path] = 1

        partial = await service.confirm(owner_id=42, token=first.operation.token)
        self.assertFalse(partial.complete)
        self.assertEqual(
            transport.calls,
            [deletion.targets[0].remote_path, failed_object.remote_path, stored_object.remote_path],
        )
        self.assertIn(manifest.remote_path, transport.files)

        await self.repo.close()
        self.repo = SQLiteJobRepository(self.database)
        await self.repo.open()
        resumed_job = await self.repo.get(job.id)
        resumed_package = await self.repo.get_archive_package(package.id)
        assert resumed_job is not None and resumed_package is not None
        resumed = ArchiveDeletionService(
            self.repo,
            transport,  # type: ignore[arg-type]
            token_factory=lambda: next(tokens),
            delete_attempts=1,
        )
        second = await resumed.prepare(resumed_job, owner_id=42)
        self.assertEqual(
            [target.target_index for target in second.status.remaining_targets],
            [failed_object.target_index, manifest.target_index],
        )
        result = await resumed.confirm(owner_id=42, token=second.operation.token)
        self.assertTrue(result.complete)
        self.assertEqual(transport.calls.count(deletion.targets[0].remote_path), 1)
        self.assertEqual(transport.calls.count(stored_object.remote_path), 1)
        self.assertEqual(transport.calls.count(failed_object.remote_path), 2)
        self.assertEqual(transport.calls.count(manifest.remote_path), 1)

    async def test_owner_and_stale_target_set_fail_closed_without_delete(self) -> None:
        job, package = await self._committed_package(item_count=1)
        transport = FakeDeleteTransport()
        service = ArchiveDeletionService(
            self.repo,
            transport,  # type: ignore[arg-type]
            token_factory=lambda: "stale-target",
        )
        confirmation = await service.prepare(job, owner_id=42)
        with self.assertRaises(ArchiveDeletionOperationInvalidError):
            await service.confirm(owner_id=7, token=confirmation.operation.token)
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                """
                UPDATE archive_deletion_targets
                SET remote_path=remote_path || '.tampered'
                WHERE package_id=? AND target_index=1
                """,
                (package.id,),
            )
        with self.assertRaises(ArchiveDeletionOperationInvalidError):
            await service.confirm(owner_id=42, token=confirmation.operation.token)
        self.assertEqual(transport.calls, [])

    async def test_unsafe_durable_package_path_never_creates_targets(self) -> None:
        job, package = await self._committed_package(item_count=1)
        async with self.repo._write_transaction() as conn:
            await conn.execute(
                "UPDATE archive_packages SET remote_path='../escape' WHERE id=?",
                (package.id,),
            )
        current_job = await self.repo.get(job.id)
        assert current_job is not None
        service = ArchiveDeletionService(
            self.repo,
            FakeDeleteTransport(),  # type: ignore[arg-type]
        )
        with self.assertRaises(ArchiveDeletionSafetyError):
            await service.prepare(current_job, owner_id=42)
        self.assertIsNone(await self.repo.get_archive_deletion(package.id))

    async def test_repository_enforces_marker_first_manifest_last_and_deleted_monotonicity(self) -> None:
        job, package = await self._committed_package(item_count=1)
        service = ArchiveDeletionService(
            self.repo,
            FakeDeleteTransport(),  # type: ignore[arg-type]
            token_factory=lambda: "repository-invariants",
        )
        await service.prepare(job, owner_id=42)
        deletion = await self.repo.get_archive_deletion(package.id)
        assert deletion is not None
        marker, media, manifest = deletion.targets
        assert marker.id is not None and media.id is not None and manifest.id is not None

        with self.assertRaisesRegex(ValueError, "commit boundary"):
            await self.repo.begin_archive_deletion_target(package.id, media.id)

        await self.repo.begin_archive_deletion_target(package.id, marker.id)
        await self.repo.checkpoint_archive_deletion_target(
            package.id,
            marker.id,
            state=ArchiveDeletionTargetState.DELETED,
            verification_method="fixture",
        )
        with self.assertRaisesRegex(ValueError, "already complete"):
            await self.repo.begin_archive_deletion_target(package.id, marker.id)
        with self.assertRaisesRegex(ValueError, "cannot become failed"):
            await self.repo.checkpoint_archive_deletion_target(
                package.id,
                marker.id,
                state=ArchiveDeletionTargetState.FAILED,
                error_code="fixture_regression",
            )

        with self.assertRaisesRegex(ValueError, "manifest must be deleted after all objects"):
            await self.repo.begin_archive_deletion_target(package.id, manifest.id)

        await self.repo.begin_archive_deletion_target(package.id, media.id)
        await self.repo.checkpoint_archive_deletion_target(
            package.id,
            media.id,
            state=ArchiveDeletionTargetState.DELETED,
            verification_method="fixture",
        )
        started_manifest = await self.repo.begin_archive_deletion_target(
            package.id,
            manifest.id,
        )
        self.assertEqual(started_manifest.kind, ArchiveDeletionTargetKind.MANIFEST)

    async def test_hanging_marker_delete_is_bounded_and_audited_as_partial(self) -> None:
        job, package = await self._committed_package(item_count=1)
        transport = FakeDeleteTransport()
        service = ArchiveDeletionService(
            self.repo,
            transport,  # type: ignore[arg-type]
            token_factory=lambda: "hanging-marker",
            delete_timeout_seconds=0.01,
            delete_attempts=1,
        )
        confirmation = await service.prepare(job, owner_id=42)
        deletion = await self.repo.get_archive_deletion(package.id)
        assert deletion is not None
        self._prime(transport, deletion)
        transport.hanging.add(deletion.targets[0].remote_path)

        result = await asyncio.wait_for(
            service.confirm(owner_id=42, token=confirmation.operation.token),
            timeout=0.5,
        )
        self.assertFalse(result.complete)
        events = await self.repo.list_archive_deletion_events(package.id)
        self.assertIn("target_delete_failed", [event.event_type for event in events])
        final = await self.repo.get_archive_deletion(package.id)
        assert final is not None
        self.assertEqual(final.state, ArchiveDeletionState.PARTIAL_FAILED)
        self.assertEqual(final.targets[0].error_code, "archive_delete_timeout")


if __name__ == "__main__":
    unittest.main()
