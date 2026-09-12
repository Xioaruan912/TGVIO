from __future__ import annotations

import asyncio
import hashlib
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from telethon import custom
from telethon.tl import functions, types

from tgvio.adapters.telegram.uploads import BoundedTelegramUploader


class FakeUploadClient:
    def __init__(self, *, delay: float = 0.0, fail_part_once: int | None = None) -> None:
        self.delay = delay
        self.fail_part_once = fail_part_once
        self.failed = False
        self.requests: list[object] = []
        self.sequential_calls: list[tuple[str, dict]] = []
        self.inflight = 0
        self.max_inflight = 0
        self.request_started = asyncio.Event()

    async def __call__(self, request):
        if not isinstance(
            request,
            (
                functions.upload.SaveFilePartRequest,
                functions.upload.SaveBigFilePartRequest,
            ),
        ):
            raise AssertionError(f"unexpected request: {request!r}")
        self.requests.append(request)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        self.request_started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if (
                self.fail_part_once is not None
                and int(request.file_part) == self.fail_part_once
                and not self.failed
            ):
                self.failed = True
                raise RuntimeError("fixture upload part failure")
            return True
        finally:
            self.inflight -= 1

    async def upload_file(self, file, **kwargs):
        self.sequential_calls.append((str(file), dict(kwargs)))
        return types.InputFile(
            id=999,
            parts=1,
            name=Path(file).name,
            md5_checksum="fallback",
        )


class BoundedTelegramUploaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _file(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    async def test_small_file_uses_save_file_parts_and_preserves_md5(self) -> None:
        payload = (b"0123456789abcdef" * 15000) + b"tail"
        source = self._file("small.bin", payload)
        client = FakeUploadClient(delay=0.001)
        uploader = BoundedTelegramUploader(
            client,
            per_file_workers=4,
            global_workers=4,
            part_size_kb=64,
        )

        uploaded = await uploader.upload(source, job_id="job-small", item_index=0)

        self.assertIsInstance(uploaded, custom.InputSizedFile)
        self.assertEqual(uploaded.size, len(payload))
        self.assertEqual(uploaded.md5_checksum, hashlib.md5(payload).hexdigest())
        expected_parts = math.ceil(len(payload) / (64 * 1024))
        self.assertEqual(uploaded.parts, expected_parts)
        self.assertEqual(len(client.requests), expected_parts)
        self.assertTrue(
            all(isinstance(request, functions.upload.SaveFilePartRequest) for request in client.requests)
        )
        self.assertEqual(
            sorted(int(request.file_part) for request in client.requests),
            list(range(expected_parts)),
        )
        self.assertGreater(client.max_inflight, 1)
        self.assertLessEqual(client.max_inflight, 4)
        self.assertEqual(client.sequential_calls, [])

    async def test_big_file_uses_save_big_file_parts(self) -> None:
        source = self.root / "big.bin"
        size = 10 * 1024 * 1024 + 123
        with source.open("wb") as handle:
            handle.seek(size - 1)
            handle.write(b"x")
        client = FakeUploadClient()
        uploader = BoundedTelegramUploader(
            client,
            per_file_workers=8,
            global_workers=8,
            part_size_kb=512,
        )

        uploaded = await uploader.upload(source)

        self.assertIsInstance(uploaded, types.InputFileBig)
        expected_parts = math.ceil(size / (512 * 1024))
        self.assertEqual(uploaded.parts, expected_parts)
        self.assertEqual(len(client.requests), expected_parts)
        self.assertTrue(
            all(
                isinstance(request, functions.upload.SaveBigFilePartRequest)
                and int(request.file_total_parts) == expected_parts
                for request in client.requests
            )
        )
        self.assertEqual(client.sequential_calls, [])

    async def test_legacy_sixteen_way_capability_is_preserved(self) -> None:
        payload = b"x" * (20 * 64 * 1024)
        source = self._file("sixteen-way.bin", payload)
        client = FakeUploadClient(delay=0.01)
        uploader = BoundedTelegramUploader(
            client,
            per_file_workers=16,
            global_workers=16,
            part_size_kb=64,
        )

        await uploader.upload(source)

        self.assertEqual(client.max_inflight, 16)
        self.assertEqual(len(client.requests), 20)

    async def test_global_limit_bounds_two_simultaneous_files(self) -> None:
        payload = b"x" * (12 * 64 * 1024)
        first = self._file("first.bin", payload)
        second = self._file("second.bin", payload)
        client = FakeUploadClient(delay=0.01)
        uploader = BoundedTelegramUploader(
            client,
            per_file_workers=8,
            global_workers=3,
            part_size_kb=64,
        )

        await asyncio.gather(uploader.upload(first), uploader.upload(second))

        self.assertGreater(client.max_inflight, 1)
        self.assertLessEqual(client.max_inflight, 3)
        self.assertEqual(len(client.requests), 24)

    async def test_concurrent_failure_falls_back_before_visible_send(self) -> None:
        source = self._file("fallback.bin", b"x" * (5 * 64 * 1024))
        client = FakeUploadClient(delay=0.002, fail_part_once=2)
        uploader = BoundedTelegramUploader(
            client,
            per_file_workers=4,
            global_workers=4,
            part_size_kb=64,
        )

        uploaded = await uploader.upload(source)

        self.assertIsInstance(uploaded, types.InputFile)
        self.assertEqual(uploaded.id, 999)
        self.assertEqual(len(client.sequential_calls), 1)
        self.assertEqual(client.inflight, 0)

    async def test_cancellation_does_not_start_sequential_fallback(self) -> None:
        source = self._file("cancel.bin", b"x" * (8 * 64 * 1024))
        client = FakeUploadClient(delay=5.0)
        uploader = BoundedTelegramUploader(
            client,
            per_file_workers=4,
            global_workers=4,
            part_size_kb=64,
        )

        task = asyncio.create_task(uploader.upload(source))
        await asyncio.wait_for(client.request_started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(client.sequential_calls, [])
        self.assertEqual(client.inflight, 0)

    def test_invalid_concurrency_and_part_sizes_fail_closed(self) -> None:
        client = FakeUploadClient()
        with self.assertRaises(ValueError):
            BoundedTelegramUploader(
                client,
                per_file_workers=0,
                global_workers=1,
                part_size_kb=512,
            )
        with self.assertRaises(ValueError):
            BoundedTelegramUploader(
                client,
                per_file_workers=1,
                global_workers=33,
                part_size_kb=512,
            )
        with self.assertRaises(ValueError):
            BoundedTelegramUploader(
                client,
                per_file_workers=1,
                global_workers=1,
                part_size_kb=500,
            )


if __name__ == "__main__":
    unittest.main()
