from __future__ import annotations

import asyncio
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest

from aiohttp.test_utils import TestClient, TestServer

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.application.auth import SessionService
from tgvio_player.application.faststart import FaststartService, build_overlay
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.domain.ranges import ByteRange
from tgvio_player.infrastructure.faststart_store import FaststartStore
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_read import ReadOnlyWebDavAdapter, WebDavRangeResponse


def box(box_type: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def moov_with_offsets(offsets: list[int], *, use_co64: bool = False) -> bytes:
    if use_co64:
        entries = b"".join(struct.pack(">Q", value) for value in offsets)
        table = box(b"co64", b"\x00\x00\x00\x00" + struct.pack(">I", len(offsets)) + entries)
    else:
        entries = b"".join(struct.pack(">I", value) for value in offsets)
        table = box(b"stco", b"\x00\x00\x00\x00" + struct.pack(">I", len(offsets)) + entries)
    stbl = box(b"stbl", table)
    minf = box(b"minf", stbl)
    mdia = box(b"mdia", minf)
    trak = box(b"trak", mdia)
    mvhd = box(b"mvhd", b"\x00" * 100)
    return box(b"moov", mvhd + trak)


def stco_offsets(moov: bytes) -> list[int]:
    marker = b"stco"
    index = moov.index(marker)
    count = int.from_bytes(moov[index + 8 : index + 12], "big")
    start = index + 12
    return [int.from_bytes(moov[start + i * 4 : start + i * 4 + 4], "big") for i in range(count)]


def make_nonfaststart(offsets: list[int] | None = None, *, use_co64: bool = False) -> tuple[bytes, list[int]]:
    ftyp = box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2avc1mp41")
    mdat_payload = bytes(range(100))
    mdat = box(b"mdat", mdat_payload)
    if offsets is None:
        data_start = len(ftyp) + 8
        offsets = [data_start, data_start + 10, data_start + 20]
    moov = moov_with_offsets(offsets, use_co64=use_co64)
    return ftyp + mdat + moov, offsets


def reader_for(buffer: bytes):
    async def read(start: int, end: int) -> bytes:
        return buffer[start : end + 1]

    return read


class FaststartOverlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_relocates_moov_and_patches_stco(self) -> None:
        buffer, offsets = make_nonfaststart()
        overlay = await build_overlay(len(buffer), "video/mp4", reader_for(buffer))
        self.assertIsNotNone(overlay)
        assert overlay is not None
        # prefix is everything before the first mdat
        prefix_len = len(box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2avc1mp41"))
        self.assertEqual(overlay.prefix_len, prefix_len)
        self.assertEqual(overlay.size, len(buffer))
        moov_len = len(buffer) - prefix_len - len(box(b"mdat", bytes(range(100))))
        self.assertEqual(overlay.moov_len, moov_len)
        self.assertEqual(stco_offsets(overlay.moov), [value + moov_len for value in offsets])

        rebuilt = b""
        for kind, source_offset, length in overlay.segments(0, overlay.size - 1):
            if kind == "cache":
                rebuilt += overlay.head[source_offset : source_offset + length]
            else:
                rebuilt += buffer[source_offset : source_offset + length]
        self.assertEqual(rebuilt[:prefix_len], buffer[:prefix_len])
        self.assertEqual(rebuilt[prefix_len : prefix_len + moov_len], overlay.moov)
        moov_start = len(buffer) - moov_len
        self.assertEqual(rebuilt[prefix_len + moov_len :], buffer[prefix_len:moov_start])
        self.assertEqual(len(rebuilt), overlay.size)

    async def test_midrange_segments_and_original_offset(self) -> None:
        buffer, _ = make_nonfaststart()
        overlay = await build_overlay(len(buffer), "video/mp4", reader_for(buffer))
        assert overlay is not None
        # A range fully inside the relocated front is served from cache.
        start = overlay.prefix_len + 5
        segments = overlay.segments(start, start + 9)
        self.assertEqual(segments, [("cache", start, 10)])
        # A range in the data region maps back to the original offset, fewer moov bytes.
        data_virtual = overlay.front_len + 7
        segments = overlay.segments(data_virtual, data_virtual + 3)
        self.assertEqual(segments, [("origin", data_virtual - overlay.moov_len, 4)])
        self.assertEqual(overlay.original_offset(data_virtual), data_virtual - overlay.moov_len)
        self.assertEqual(overlay.original_offset(0), -1)

    async def test_co64_variant(self) -> None:
        buffer, offsets = make_nonfaststart(use_co64=True)
        overlay = await build_overlay(len(buffer), "video/mp4", reader_for(buffer))
        assert overlay is not None
        moov_len = overlay.moov_len
        index = overlay.moov.index(b"co64")
        count = int.from_bytes(overlay.moov[index + 8 : index + 12], "big")
        patched = [
            int.from_bytes(overlay.moov[index + 12 + i * 8 : index + 12 + i * 8 + 8], "big")
            for i in range(count)
        ]
        self.assertEqual(patched, [value + moov_len for value in offsets])

    async def test_already_faststart_is_ignored(self) -> None:
        ftyp = box(b"ftyp", b"isom" + b"\x00\x00\x02\x00")
        moov = moov_with_offsets([100])
        mdat = box(b"mdat", bytes(50))
        overlay = await build_overlay(len(ftyp + moov + mdat), "video/mp4", reader_for(ftyp + moov + mdat))
        self.assertIsNone(overlay)

    async def test_trailing_box_after_moov_is_ignored(self) -> None:
        ftyp = box(b"ftyp", b"isom" + b"\x00\x00\x02\x00")
        mdat = box(b"mdat", bytes(50))
        moov = moov_with_offsets([16])
        buffer = ftyp + mdat + moov + box(b"free", b"")
        overlay = await build_overlay(len(buffer), "video/mp4", reader_for(buffer))
        self.assertIsNone(overlay)

    async def test_out_of_range_offsets_are_rejected(self) -> None:
        buffer, _ = make_nonfaststart(offsets=[10, 20])
        overlay = await build_overlay(len(buffer), "video/mp4", reader_for(buffer))
        self.assertIsNone(overlay)

    async def test_non_mp4_mime_is_ignored(self) -> None:
        buffer, _ = make_nonfaststart()
        self.assertIsNone(await build_overlay(len(buffer), "image/jpeg", reader_for(buffer)))


class FakeBufferClient:
    def __init__(self, buffer: bytes) -> None:
        self.buffer = buffer

    async def open_range(self, remote_path: str, byte_range: ByteRange | None) -> WebDavRangeResponse:
        class Body:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload
                self.sent = False

            def __aiter__(self):
                return self

            async def __anext__(self) -> bytes:
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return self.payload

            async def aclose(self) -> None:
                return None

        if byte_range is None:
            payload = self.buffer
            return WebDavRangeResponse(200, "video/mp4", len(payload), None, None, Body(payload))
        payload = self.buffer[byte_range.start : byte_range.end + 1]
        return WebDavRangeResponse(
            206,
            "video/mp4",
            len(payload),
            f"bytes {byte_range.start}-{byte_range.end}/{len(self.buffer)}",
            None,
            Body(payload),
        )


class FaststartStoreTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        from tgvio_player.application.faststart import FaststartOverlay

        with TemporaryDirectory() as temporary:
            store = FaststartStore(Path(temporary) / "faststart")
            head = b"P" * 32 + b"moovdata"
            overlay = FaststartOverlay(prefix_len=32, head=head, size=1000, mime="video/mp4")
            store.save("a" * 64, overlay)
            loaded = store.load("a" * 64)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(
                (loaded.prefix_len, loaded.head, loaded.moov, loaded.size, loaded.mime),
                (32, head, b"moovdata", 1000, "video/mp4"),
            )
            self.assertIsNone(store.load("b" * 64))


class FaststartHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()
        self.buffer, _offsets = make_nonfaststart()
        self.media_id = "c" * 64
        package = CatalogPackage(
            "package",
            "TGVIO/2026-09-22/1",
            "b" * 64,
            '"manifest"',
            '"complete"',
            (CatalogMedia(self.media_id, "video", len(self.buffer), "video/mp4", 1080, 1920, 12.0),),
            (CatalogLocation(self.media_id, "package", "video.mp4", '"etag"'),),
        )
        await self.repo.apply_package(package)
        await self.repo.refresh_media_activity()
        self.reader = ReadOnlyWebDavAdapter(FakeBufferClient(self.buffer))
        self.faststart = FaststartService(
            FaststartStore(Path(self.tmp.name) / "faststart"), self.repo, self.reader
        )
        self.server = PlayerHttpServer(
            self.repo,
            SessionService(self.repo, access_secret="s" * 32),
            ShuffleDeckService(self.repo),
            self.reader,
            faststart=self.faststart,
        )
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.repo.close()
        self.tmp.cleanup()

    async def _login(self) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"secret": "s" * 32})
        self.assertEqual(response.status, 200)
        return response.cookies["tgvio_player_session"].value

    async def test_open_ended_stream_starts_with_ftyp_then_moov(self) -> None:
        cookie = await self._login()
        details = await self.repo.active_media_details(self.media_id)
        assert details is not None
        await self.faststart.overlay_for(self.media_id, details)
        response = await self.client.get(
            f"/api/v1/media/{self.media_id}/stream",
            headers={"Range": "bytes=0-"},
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 206)
        payload = await response.read()
        self.assertEqual(len(payload), len(self.buffer))
        ftyp = box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2avc1mp41")
        self.assertEqual(payload[: len(ftyp)], ftyp)
        self.assertEqual(payload[len(ftyp) : len(ftyp) + 8][4:8], b"moov")
        self.assertEqual(response.headers["Content-Range"], f"bytes 0-{len(self.buffer) - 1}/{len(self.buffer)}")

    async def test_prepare_endpoint_builds_the_overlay(self) -> None:
        cookie = await self._login()
        details = await self.repo.active_media_details(self.media_id)
        assert details is not None
        response = await self.client.post(
            f"/api/v1/media/{self.media_id}/prepare",
            cookies={"tgvio_player_session": cookie},
        )
        self.assertEqual(response.status, 202)
        for _ in range(100):
            if self.faststart.peek(self.media_id, details) is not None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(self.faststart.peek(self.media_id, details))


if __name__ == "__main__":
    unittest.main()
