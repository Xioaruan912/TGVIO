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
    async def test_first_bytes_are_streamed_before_a_full_cache_chunk_arrives(self) -> None:
        payload = bytes(range(64))
        first_piece_sent = asyncio.Event()
        release_remainder = asyncio.Event()

        class Reader:
            async def open_range(self, package: str, relpath: str, byte_range: ByteRange):
                class Body:
                    def __init__(self) -> None:
                        self.parts = [payload[:3], payload[3:byte_range.length]]

                    def __aiter__(self):
                        return self

                    async def __anext__(self) -> bytes:
                        if not self.parts:
                            raise StopAsyncIteration
                        part = self.parts.pop(0)
                        if not self.parts:
                            first_piece_sent.set()
                            await release_remainder.wait()
                        return part

                    async def aclose(self) -> None:
                        return None

                class Response:
                    status = 206
                    content_length = byte_range.length
                    body = Body()

                return Response()

        cache = MediaRangeCache(FakeStore(), Reader(), window_bytes=64, max_attempts=1)
        iterator = cache.stream("k", "pkg", "clip.mp4", len(payload), ByteRange(0, 7)).__aiter__()
        first = asyncio.create_task(iterator.__anext__())
        try:
            await first_piece_sent.wait()
            await asyncio.sleep(0)
            self.assertTrue(first.done(), "browser should receive available bytes before the 8-byte cache chunk completes")
            self.assertEqual(await first, payload[:3])
        finally:
            release_remainder.set()
            await cache.shutdown()

    async def test_prime_waits_for_a_small_prefix_not_the_entire_cache_chunk(self) -> None:
        chunk_bytes = 256 * 1024
        prefix_bytes = 64 * 1024
        payload = bytes(index % 251 for index in range(chunk_bytes * 2))
        first_piece_sent = asyncio.Event()
        release_remainder = asyncio.Event()

        class Reader:
            async def open_range(self, package: str, relpath: str, byte_range: ByteRange):
                class Body:
                    def __init__(self) -> None:
                        self.parts = [payload[:prefix_bytes], payload[prefix_bytes:byte_range.length]]
                        self.first = True

                    def __aiter__(self):
                        return self

                    async def __anext__(self) -> bytes:
                        if not self.parts:
                            raise StopAsyncIteration
                        part = self.parts.pop(0)
                        if self.first:
                            self.first = False
                            first_piece_sent.set()
                            return part
                        await release_remainder.wait()
                        return part

                    async def aclose(self) -> None:
                        return None

                class Response:
                    status = 206
                    content_length = byte_range.length
                    body = Body()

                return Response()

        store = FakeStore()
        store.chunk_bytes = chunk_bytes
        cache = MediaRangeCache(store, Reader(), window_bytes=len(payload), max_attempts=1)
        prime = asyncio.create_task(
            cache.prime("k", "pkg", "clip.mp4", len(payload), ByteRange(0, len(payload) - 1))
        )
        try:
            await first_piece_sent.wait()
            await asyncio.sleep(0)
            self.assertTrue(prime.done(), "a 64 KiB prefix is enough to start the HTTP response")
            await prime
        finally:
            release_remainder.set()
            await cache.shutdown()

    async def test_truncated_upstream_window_fails_instead_of_waiting_forever(self) -> None:
        class Reader:
            async def open_range(self, package: str, relpath: str, byte_range: ByteRange):
                return _Response(206, b"short")

        cache = MediaRangeCache(
            FakeStore(), Reader(), max_attempts=1, backoff_seconds=0.01
        )
        try:
            with self.assertRaises(Exception):
                await asyncio.wait_for(
                    collect(cache.stream("k", "pkg", "clip.mp4", 64, ByteRange(0, 7))),
                    timeout=0.2,
                )
        finally:
            await cache.shutdown()

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
        cache = MediaRangeCache(FakeStore(), reader, concurrency=2)
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
        cache = MediaRangeCache(FakeStore(), FakeReader(buffer), concurrency=2)
        cache.prefetch_head("k", "pkg", "clip.mp4", len(buffer), 999)
        for _ in range(50):
            if ("k", 0) in cache._store.data:
                break
            await asyncio.sleep(0.01)
        self.assertIn(("k", 0), cache._store.data)
        await cache.shutdown()


class _Body:
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


class _Response:
    def __init__(self, status: int, payload: bytes = b"") -> None:
        self.status = status
        self.content_length = len(payload)
        self.content_type = None
        self.content_range = None
        self.etag = None
        self.body = _Body(payload)


class ThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def test_throttling_is_retried(self) -> None:
        buffer = bytes(range(64))

        class Reader:
            def __init__(self) -> None:
                self.calls = 0

            async def open_range(self, package: str, relpath: str, byte_range: ByteRange):
                self.calls += 1
                if self.calls == 1:
                    return _Response(403)
                return _Response(206, buffer[byte_range.start : byte_range.end + 1])

        reader = Reader()
        cache = MediaRangeCache(
            FakeStore(), reader, concurrency=4, max_attempts=4, backoff_seconds=0.01
        )
        result = await collect(
            cache.stream("k", "pkg", "clip.mp4", len(buffer), ByteRange(0, 7))
        )
        self.assertEqual(result, buffer[0:8])
        self.assertGreaterEqual(reader.calls, 2)

    async def test_persistent_throttling_raises(self) -> None:
        class Reader:
            async def open_range(self, package: str, relpath: str, byte_range: ByteRange):
                return _Response(403)

        cache = MediaRangeCache(
            FakeStore(), Reader(), concurrency=1, max_attempts=2, backoff_seconds=0.01
        )
        with self.assertRaises(Exception):
            await collect(cache.stream("k", "pkg", "clip.mp4", 64, ByteRange(0, 7)))
        await cache.shutdown()

    async def test_failed_window_can_be_retried_by_a_later_playback_request(self) -> None:
        payload = bytes(range(64))

        class Reader:
            failed = True

            async def open_range(self, package: str, relpath: str, byte_range: ByteRange):
                if self.failed:
                    return _Response(403)
                return _Response(206, payload[byte_range.start : byte_range.end + 1])

        reader = Reader()
        cache = MediaRangeCache(
            FakeStore(), reader, max_attempts=1, backoff_seconds=0.01
        )
        try:
            with self.assertRaises(Exception):
                await collect(cache.stream("k", "pkg", "clip.mp4", 64, ByteRange(0, 7)))
            reader.failed = False
            retried = await asyncio.wait_for(
                collect(cache.stream("k", "pkg", "clip.mp4", 64, ByteRange(0, 7))),
                timeout=0.2,
            )
            self.assertEqual(retried, payload[:8])
        finally:
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
