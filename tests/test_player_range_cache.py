from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio_player.application.range_cache import MediaRangeCache
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.infrastructure.range_store import RangeStore


class FakeStore:
    chunk_bytes = 8

    def __init__(self) -> None:
        self.data: dict[tuple[str, int], bytes] = {}

    def open(self) -> None:
        return None

    def read_slice(self, key: str, index: int, offset: int, length: int) -> bytes | None:
        data = self.data.get((key, index))
        return None if data is None else data[offset : offset + length]

    def write_chunk(self, key: str, index: int, data: bytes) -> None:
        self.data[(key, index)] = data

    def stats(self) -> dict[str, object]:
        return {"bytes": sum(len(v) for v in self.data.values()), "chunks": len(self.data)}


class FakeReader:
    def __init__(self, buffer: bytes) -> None:
        self.buffer = buffer
        self.calls: list[tuple[int, int]] = []

    async def open_range(self, package: str, relpath: str, byte_range: ByteRange):
        self.calls.append((byte_range.start, byte_range.end))

        class Body:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload
                self.done = False

            def __aiter__(self):
                return self

            async def __anext__(self) -> bytes:
                if self.done:
                    raise StopAsyncIteration
                self.done = True
                return self.payload

            async def aclose(self) -> None:
                return None

        payload = self.buffer[byte_range.start : byte_range.end + 1]

        class Response:
            status = 206
            content_length = len(payload)
            body = Body(payload)

        return Response()


async def collect(agen) -> bytes:
    return b"".join([chunk async for chunk in agen])


class MediaRangeCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_reads_through_the_cache(self) -> None:
        buffer = bytes(range(100))
        reader = FakeReader(buffer)
        cache = MediaRangeCache(FakeStore(), reader)
        first = await collect(cache.stream("k", "pkg", "clip.mp4", len(buffer), ByteRange(0, 19)))
        self.assertEqual(first, buffer[0:20])
        calls_after_first = list(reader.calls)
        self.assertTrue(calls_after_first)
        second = await collect(cache.stream("k", "pkg", "clip.mp4", len(buffer), ByteRange(5, 20)))
        self.assertEqual(second, buffer[5:21])
        # The overlapping chunks are now cached, so no new upstream reads.
        self.assertEqual(reader.calls, calls_after_first)

    async def test_concurrent_chunk_read_is_deduplicated(self) -> None:
        buffer = bytes(range(64))
        reader = FakeReader(buffer)
        cache = MediaRangeCache(FakeStore(), reader)
        results = await asyncio.gather(
            collect(cache.stream("k", "pkg", "clip.mp4", len(buffer), ByteRange(0, 7))),
            collect(cache.stream("k", "pkg", "clip.mp4", len(buffer), ByteRange(0, 7))),
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(reader.calls), 1)

    async def test_prime_validates_upstream(self) -> None:
        buffer = bytes(range(32))
        cache = MediaRangeCache(FakeStore(), FakeReader(buffer))
        await cache.prime("k", "pkg", "clip.mp4", len(buffer), ByteRange(0, 7))

    async def test_prefetch_populates_following_chunks(self) -> None:
        buffer = bytes(range(64))
        reader = FakeReader(buffer)
        cache = MediaRangeCache(FakeStore(), reader, prefetch_chunks=2, prefetch_sleep=0)
        await collect(cache.stream("k", "pkg", "clip.mp4", len(buffer), ByteRange(0, 7), prefetch=True))
        for _ in range(50):
            if all(("k", i) in cache._store.data for i in range(3)):
                break
            await asyncio.sleep(0.01)
        self.assertIn(("k", 1), cache._store.data)
        self.assertIn(("k", 2), cache._store.data)
        await cache.shutdown()


    async def test_prefetch_head_warms_the_start(self) -> None:
        buffer = bytes(range(64))
        cache = MediaRangeCache(FakeStore(), FakeReader(buffer), prefetch_chunks=2, prefetch_sleep=0)
        cache.prefetch_head("k", "pkg", "clip.mp4", len(buffer), 999)
        for _ in range(50):
            if ("k", 0) in cache._store.data:
                break
            await asyncio.sleep(0.01)
        self.assertIn(("k", 0), cache._store.data)
        await cache.shutdown()


class RangeStoreTests(unittest.TestCase):
    def test_write_read_and_lru_eviction(self) -> None:
        with TemporaryDirectory() as temporary:
            store = RangeStore(
                Path(temporary) / "cache", chunk_bytes=64 * 1024, max_bytes=2 * 64 * 1024
            )
            store.open()
            chunk = b"a" * (64 * 1024)
            store.write_chunk("k", 0, chunk)
            store.write_chunk("k", 1, chunk)
            self.assertEqual(store.read_slice("k", 0, 0, 4), b"aaaa")
            store.write_chunk("k", 2, chunk)  # exceeds 2 chunks -> evicts the LRU chunk
            self.assertLessEqual(store.total_bytes, 2 * 64 * 1024)
            self.assertTrue(store.has("k", 2))
            self.assertLessEqual(sum([store.has("k", 0), store.has("k", 1)]), 1)

    def test_open_scans_existing_chunks(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            store = RangeStore(root, chunk_bytes=64 * 1024, max_bytes=8 * 64 * 1024)
            store.open()
            store.write_chunk("k", 3, b"x" * 10)
            reopened = RangeStore(root, chunk_bytes=64 * 1024, max_bytes=8 * 64 * 1024)
            reopened.open()
            self.assertTrue(reopened.has("k", 3))
            self.assertEqual(reopened.total_bytes, 10)


if __name__ == "__main__":
    unittest.main()
