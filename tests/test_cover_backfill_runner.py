from __future__ import annotations

import json
import unittest

from tgvio.application.cover_backfill import COVERS_SCHEMA, CoverBackfill
from tgvio.application.cover_backfill_runner import (
    CoverBackfillRunner,
    PackageRef,
    index_is_present,
)


def _video(index: int) -> dict[str, object]:
    return {"index": index, "kind": "video", "path": f"media/{index:03d}__clip.mp4"}


class _Port:
    def __init__(self, packages: list[PackageRef], stored: dict[str, bytes] | None = None) -> None:
        self._packages = packages
        self.stored = dict(stored or {})
        self.written: list[tuple[str, bytes]] = []
        self.reads: list[str] = []
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None

    async def packages(self, limit: int | None):
        return self._packages[: limit or len(self._packages)]

    async def read(self, relpath: str, *, max_bytes: int) -> bytes | None:
        self.reads.append(relpath)
        if self.read_error is not None:
            raise self.read_error
        return self.stored.get(relpath)

    async def write(self, relpath: str, payload: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.written.append((relpath, payload))


class CoverBackfillRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _backfill(self, *, frame: bytes | None = b"jpeg") -> CoverBackfill:
        async def sample(_source: str) -> bytes | None:
            return frame

        return CoverBackfill(sample)

    def _package(self, package_id: str = "pkg-1", media=None) -> PackageRef:
        return PackageRef(
            package_id=package_id,
            remote_path=f"TGVIO/2026/09/22/{package_id}",
            manifest={"media": media if media is not None else [_video(1), _video(2)]},
        )

    async def test_a_package_without_an_index_is_covered_with_the_index_last(self) -> None:
        port = _Port([self._package()])
        run = await CoverBackfillRunner(port, self._backfill()).run()
        self.assertEqual(run.as_dict(), {
            "packages_seen": 1, "packages_skipped": 0, "packages_updated": 1,
            "covers_written": 2, "failures": 0,
        })
        roles = [
            "index" if relpath.endswith("covers.json") else "cover"
            for relpath, _ in port.written
        ]
        self.assertEqual(roles, ["cover", "cover", "index"])
        index = json.loads(port.written[-1][1])
        self.assertEqual(index["schema"], COVERS_SCHEMA)
        self.assertEqual(len(index["covers"]), 2)

    async def test_a_package_that_already_carries_an_index_is_never_sampled_again(self) -> None:
        package = self._package()
        stored = {
            f"{package.remote_path}/covers.json": json.dumps(
                {"schema": COVERS_SCHEMA, "algorithm": "x", "covers": {}}
            ).encode()
        }
        port = _Port([package], stored)
        sampled: list[str] = []

        async def sample(source: str) -> bytes | None:
            sampled.append(source)
            return b"jpeg"

        run = await CoverBackfillRunner(port, CoverBackfill(sample)).run()
        self.assertEqual(sampled, [], "an existing index makes the whole package a no-op")
        self.assertEqual(port.written, [])
        self.assertEqual(run.skipped, 1)

    async def test_non_video_and_empty_packages_are_skipped_without_writing(self) -> None:
        port = _Port([
            self._package("no-video", media=[{"index": 1, "kind": "photo", "path": "media/a.jpg"}]),
            self._package("empty", media=[]),
        ])
        run = await CoverBackfillRunner(port, self._backfill()).run()
        self.assertEqual(port.written, [])
        self.assertEqual(run.skipped, 2)
        self.assertEqual(run.updated, 0)

    async def test_dry_run_reports_without_writing_anything(self) -> None:
        port = _Port([self._package()])
        run = await CoverBackfillRunner(port, self._backfill(), dry_run=True).run()
        self.assertEqual(port.written, [])
        self.assertEqual(run.updated, 1)
        self.assertEqual(run.covers, 2)

    async def test_a_write_failure_is_counted_and_does_not_stop_later_packages(self) -> None:
        first, second = self._package("pkg-1"), self._package("pkg-2")
        port = _Port([first, second])
        port.write_error = RuntimeError("webdav down")
        run = await CoverBackfillRunner(port, self._backfill()).run()
        self.assertEqual(run.seen, 2)
        self.assertEqual(run.failures, 2, "both packages failed but both were attempted")
        port.write_error = None
        resumed = _Port([first, second])
        run_again = await CoverBackfillRunner(resumed, self._backfill()).run()
        self.assertEqual(run_again.updated, 2, "the same packages complete once the transport recovers")

    async def test_an_unreadable_or_junk_index_is_treated_as_absent(self) -> None:
        port = _Port([self._package()])
        port.read_error = RuntimeError("stat failed")
        self.assertEqual((await CoverBackfillRunner(port, self._backfill()).run()).updated, 1)
        self.assertFalse(index_is_present(b"not json"))
        self.assertFalse(index_is_present(b""))
        self.assertFalse(index_is_present(None))
        self.assertFalse(index_is_present(json.dumps({"schema": "other"}).encode()))
        self.assertTrue(index_is_present(json.dumps({"schema": COVERS_SCHEMA, "covers": {}}).encode()))

    async def test_the_limit_bounds_how_many_packages_are_touched(self) -> None:
        port = _Port([self._package("a"), self._package("b"), self._package("c")])
        run = await CoverBackfillRunner(port, self._backfill(), limit=2).run()
        self.assertEqual(run.seen, 2)


if __name__ == "__main__":
    unittest.main()
