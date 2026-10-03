"""The cover mirror warms itself: bounded, resumable, and never in playback's way.

The warm loop is not on any request path. What it must not do is starve the request path:
it opens at most half the cover lanes, takes one batch per round, stops when the disk budget
is full, and treats "the source is gone" as an answer rather than something to retry.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.application.cover_warm import CATCH_UP_SECONDS, CoverWarm
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.infrastructure.cover_mirror import CoverMirror, CoverMirrorCounters
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse

PAYLOAD = b"12345678"
DIGESTS = [f"{n:064x}" for n in range(1, 9)]
ROWS = [(f"media-{n}", "package", f"cover/backfill/{digest}.jpg", len(PAYLOAD))
        for n, digest in enumerate(DIGESTS, start=1)]


class ClosableBody:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class FakeReader:
    """Records every read, can fail transiently, and can declare a source gone."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.transient: dict[str, int] = {}
        self.gone: set[str] = set()
        self.active = 0
        self.peak = 0

    async def open_range(self, remote_path: str, remote_relpath: str, byte_range: ByteRange):
        digest = remote_relpath.split("/")[-1].removesuffix(".jpg")
        self.calls.append(remote_relpath)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            if self.transient.get(digest, 0) > 0:
                self.transient[digest] -= 1
                raise OSError("transient")
            if digest in self.gone:
                return WebDavRangeResponse(404, "image/jpeg", 0, None, None, ClosableBody([]))
            return WebDavRangeResponse(206, "image/jpeg", len(PAYLOAD), None, '"e"', ClosableBody([PAYLOAD]))
        finally:
            self.active -= 1


class FakeRepository:
    def __init__(self, rows=ROWS) -> None:
        self.rows = rows

    async def mirror_candidates(self):
        return list(self.rows)


class CoverWarmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sleeps: list[float] = []

    def warm(self, *, mirror=None, reader=None, batch=64, concurrency=2, idle_seconds=900):
        self.mirror = mirror or CoverMirror(Path(self.tmp.name) / "covers", budget_bytes=1024)
        self.counters = CoverMirrorCounters()
        self.reader = reader or FakeReader()

        async def sleep(stop, seconds):
            self.sleeps.append(seconds)
            await asyncio.sleep(0)

        return CoverWarm(
            FakeRepository(), self.reader, self.mirror, self.counters,
            batch=batch, concurrency=concurrency, idle_seconds=idle_seconds,
            retry_delays=(0.0,), sleep=sleep,
        )

    async def test_only_covers_missing_locally_are_fetched(self) -> None:
        warm = self.warm()
        self.mirror.write(DIGESTS[0], PAYLOAD)
        run = await warm.run_once()
        self.assertEqual(run.fetched, len(ROWS) - 1, "a cover already mirrored is not fetched again")
        self.assertNotIn(ROWS[0][2], self.reader.calls)
        self.assertEqual(run.skipped, 1)

    async def test_concurrency_never_exceeds_the_lane_limit(self) -> None:
        warm = self.warm(concurrency=2)
        run = await warm.run_once()
        self.assertEqual(run.fetched, len(ROWS))
        self.assertLessEqual(self.reader.peak, 2, "the warm loop never opens more lanes than allowed")

    async def test_a_transient_failure_is_retried_then_skipped(self) -> None:
        reader = FakeReader()
        reader.transient[DIGESTS[1]] = 1              # one retry is enough
        reader.transient[DIGESTS[2]] = 99             # never succeeds this round
        warm = self.warm(reader=reader)
        run = await warm.run_once()
        self.assertTrue(self.mirror.exists(DIGESTS[1]), "a transient failure is retried")
        self.assertFalse(self.mirror.exists(DIGESTS[2]))
        self.assertEqual(run.failed, 1)
        self.assertEqual(run.fetched, len(ROWS) - 1)

    async def test_a_missing_source_is_tried_once(self) -> None:
        reader = FakeReader()
        reader.gone.add(DIGESTS[3])
        warm = self.warm(reader=reader)
        run = await warm.run_once()
        self.assertEqual(self.reader.calls.count(ROWS[3][2]), 1, "a gone source is not hammered")
        self.assertEqual(run.failed, 1)
        self.assertEqual(self.counters.warm_failed, 1)

    async def test_the_run_stops_at_the_disk_budget(self) -> None:
        mirror = CoverMirror(Path(self.tmp.name) / "small", budget_bytes=len(PAYLOAD) * 2)
        warm = self.warm(mirror=mirror)
        run = await warm.run_once()
        self.assertEqual(run.fetched, 2, "two covers fill a two-cover budget and the round stops")
        self.assertEqual(mirror.stats()[1], len(PAYLOAD) * 2)

    async def test_the_cadence_chases_a_backlog_and_backs_off_when_idle(self) -> None:
        warm = self.warm()
        stop = asyncio.Event()
        task = asyncio.create_task(warm.run(stop))
        # The loop hops through threads, so yielding a fixed number of times is not enough;
        # wait for the two cadences it must choose between.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if CATCH_UP_SECONDS in self.sleeps and 900 in self.sleeps:
                break
        stop.set()
        await task
        self.assertIn(CATCH_UP_SECONDS, self.sleeps, "a backlog is chased")
        self.assertIn(900, self.sleeps, "an empty pass backs off")

    async def test_the_sweep_keeps_every_active_cover_not_just_this_batch(self) -> None:
        warm = self.warm(batch=1)
        self.mirror.write(DIGESTS[7], PAYLOAD)        # active, but never in a batch of one
        await warm.run_once()
        self.assertTrue(self.mirror.exists(DIGESTS[7]), "an active cover outside the batch is kept")
