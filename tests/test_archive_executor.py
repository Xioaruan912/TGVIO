from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.archive_executor import ArchiveExecutor
from tgvio.application.archive_planner import ArchivePlanner
from tgvio.domain.archive import (
    ArchiveCapabilities,
    ArchiveObjectState,
    ArchivePackageState,
    ArchiveRemoteStat,
    ArchiveStoreReceipt,
    archive_complete_marker,
    archive_json_bytes,
)
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class MemoryArchiveTransport:
    def __init__(self, *, move: bool = False) -> None:
        self.capabilities = ArchiveCapabilities(
            supports_propfind=True,
            supports_mkcol=True,
            supports_put=True,
            supports_move=move,
            supports_get=True,
            supports_etag=True,
        )
        self.files: dict[str, bytes] = {}
        self.collections: set[str] = set()
        self.put_counts: dict[str, int] = {}
        self.write_order: list[str] = []
        self.move_calls: list[tuple[str, str]] = []
        self.fail_once_suffix: str | None = None
        self.fail_complete_once = False

    async def probe(self):
        return self.capabilities

    async def ensure_collection(self, remote_path: str) -> None:
        self.collections.add(remote_path.rstrip("/"))

    async def stat(self, remote_path: str) -> ArchiveRemoteStat:
        if remote_path in self.files:
            payload = self.files[remote_path]
            return ArchiveRemoteStat(exists=True, size_bytes=len(payload), etag=f'"{len(payload)}"')
        if remote_path.rstrip("/") in self.collections:
            return ArchiveRemoteStat(exists=True, is_collection=True)
        return ArchiveRemoteStat(exists=False)

    async def put_file(self, local_path: Path, remote_path: str, expected_size: int):
        self.put_counts[remote_path] = self.put_counts.get(remote_path, 0) + 1
        if self.fail_once_suffix and remote_path.endswith(self.fail_once_suffix):
            self.fail_once_suffix = None
            raise TimeoutError("fixture transfer failed")
        payload = Path(local_path).read_bytes()
        if len(payload) != expected_size:
            raise AssertionError("fixture size mismatch")
        self.files[remote_path] = payload
        self.write_order.append(remote_path)
        return ArchiveStoreReceipt(
            remote_path=remote_path,
            size_bytes=len(payload),
            verification_method="size",
            etag=f'"{len(payload)}"',
        )

    async def put_bytes(self, payload: bytes, remote_path: str, *, content_type: str):
        if self.fail_complete_once and remote_path.endswith("/_COMPLETE.json"):
            self.fail_complete_once = False
            raise TimeoutError("fixture marker failure")
        self.files[remote_path] = bytes(payload)
        self.write_order.append(remote_path)
        return ArchiveStoreReceipt(
            remote_path=remote_path,
            size_bytes=len(payload),
            verification_method="content",
        )

    async def get_bytes(self, remote_path: str, *, max_bytes: int):
        value = self.files.get(remote_path)
        if value is None:
            return None
        if len(value) > max_bytes:
            raise RuntimeError("fixture read limit")
        return value

    async def move_collection(self, source_path: str, destination_path: str) -> None:
        self.move_calls.append((source_path, destination_path))
        prefix = source_path.rstrip("/") + "/"
        moved = {
            destination_path.rstrip("/") + "/" + path[len(prefix):]: payload
            for path, payload in list(self.files.items())
            if path.startswith(prefix)
        }
        for path in [path for path in self.files if path.startswith(prefix)]:
            del self.files[path]
        self.files.update(moved)


class ProbeFailArchiveTransport(MemoryArchiveTransport):
    async def probe(self):
        raise TimeoutError("fixture probe timeout")


class ArchiveExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = SQLiteJobRepository(self.root / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _package(self, count: int = 2):
        items = []
        for index in range(count):
            path = self.root / f"media-{index}.bin"
            payload = (f"archive-payload-{index}" * 3).encode()
            path.write_bytes(payload)
            items.append(
                MediaItem(
                    index=index,
                    kind=MediaKind.DOCUMENT,
                    source=f"fixture:{index}",
                    local_path=str(path),
                    name=f"file {index + 1}.bin",
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
        job = Job(
            id="9" * 32,
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=items,
            created_at="2026-09-04 01:19:23",
        )
        await self.repo.create(job)
        return await self.repo.save_archive_plan(ArchivePlanner().plan(job))

    async def test_probe_failure_is_checkpointed_as_failed_and_durable_probe_status(self) -> None:
        package = await self._package()
        transport = ProbeFailArchiveTransport()
        with self.assertRaises(TimeoutError):
            await ArchiveExecutor(self.repo, transport).execute(package)

        failed = await self.repo.get_archive_package(package.id)
        assert failed is not None
        self.assertEqual(failed.state, ArchivePackageState.FAILED)
        self.assertEqual(failed.error_code, "archive_probe_failed")
        health = await self.repo.get_runtime_health()
        self.assertEqual(health["archive_probe"]["status"], "unreachable")
        self.assertEqual(health["archive_probe"]["detail"]["profile_id"], "primary")
        self.assertNotIn("archive_capability", health)

    async def test_successful_execution_persists_capability_snapshot(self) -> None:
        package = await self._package()
        transport = MemoryArchiveTransport(move=True)
        await ArchiveExecutor(self.repo, transport).execute(package)
        health = await self.repo.get_runtime_health()
        self.assertEqual(health["archive_capability"]["status"], "confirmed")
        self.assertEqual(health["archive_capability"]["detail"]["profile_id"], "primary")
        self.assertEqual(health["archive_capability"]["detail"]["commit_mode"], "move")
        self.assertEqual(health["archive_probe"]["status"], "reachable")

    async def test_marker_commit_writes_final_tree_and_complete_marker_last(self) -> None:
        package = await self._package()
        transport = MemoryArchiveTransport(move=False)
        completed = await ArchiveExecutor(self.repo, transport).execute(package)
        self.assertEqual(completed.state, ArchivePackageState.COMMITTED)
        self.assertTrue(all(obj.state == ArchiveObjectState.STORED for obj in completed.objects))
        self.assertEqual(transport.move_calls, [])
        self.assertTrue(
            all(
                f"{package.remote_path}/{obj.remote_relpath}" in transport.files
                for obj in package.objects
            )
        )
        self.assertEqual(transport.write_order[-1], f"{package.remote_path}/_COMPLETE.json")
        self.assertEqual(
            transport.files[f"{package.remote_path}/_COMPLETE.json"],
            archive_json_bytes(archive_complete_marker(completed)),
        )

    async def test_move_capability_uses_staging_then_collection_move(self) -> None:
        package = await self._package()
        transport = MemoryArchiveTransport(move=True)
        completed = await ArchiveExecutor(self.repo, transport).execute(package)
        self.assertEqual(completed.state, ArchivePackageState.COMMITTED)
        self.assertEqual(transport.move_calls, [(package.staging_path, package.remote_path)])
        self.assertFalse(
            any(path.startswith(package.staging_path + "/") for path in transport.files)
        )
        self.assertIn(f"{package.remote_path}/manifest.json", transport.files)
        self.assertIn(f"{package.remote_path}/_COMPLETE.json", transport.files)

    async def test_failed_object_resumes_without_reuploading_stored_object(self) -> None:
        package = await self._package()
        transport = MemoryArchiveTransport(move=False)
        transport.fail_once_suffix = package.objects[1].remote_relpath
        executor = ArchiveExecutor(self.repo, transport)
        with self.assertRaises(TimeoutError):
            await executor.execute(package)
        failed = await self.repo.get_archive_package(package.id)
        assert failed is not None
        self.assertEqual(failed.state, ArchivePackageState.FAILED)
        self.assertEqual(failed.objects[0].state, ArchiveObjectState.STORED)
        self.assertEqual(failed.objects[1].state, ArchiveObjectState.FAILED)
        first_path = f"{package.remote_path}/{package.objects[0].remote_relpath}"
        self.assertEqual(transport.put_counts[first_path], 1)

        completed = await executor.execute(failed)
        self.assertEqual(completed.state, ArchivePackageState.COMMITTED)
        self.assertEqual(transport.put_counts[first_path], 1)

    async def test_crash_after_move_before_marker_resumes_from_final_tree(self) -> None:
        package = await self._package()
        transport = MemoryArchiveTransport(move=True)
        transport.fail_complete_once = True
        executor = ArchiveExecutor(self.repo, transport)
        with self.assertRaises(TimeoutError):
            await executor.execute(package)
        failed = await self.repo.get_archive_package(package.id)
        assert failed is not None
        self.assertEqual(failed.state, ArchivePackageState.FAILED)
        self.assertIn(f"{package.remote_path}/manifest.json", transport.files)
        self.assertEqual(len(transport.move_calls), 1)

        completed = await executor.execute(failed)
        self.assertEqual(completed.state, ArchivePackageState.COMMITTED)
        self.assertEqual(len(transport.move_calls), 1)
