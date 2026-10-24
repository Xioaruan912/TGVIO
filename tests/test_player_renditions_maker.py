from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

import aiohttp

from tgvio_player.domain.catalog import CatalogLocation, CatalogMedia, CatalogPackage
from tgvio_player.infrastructure.folder_catalog import LibraryCatalogSource, plain_media_id, rendition_media_id
from tgvio_player.infrastructure.rendition_maker import RenditionMaker, _SourceServer
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_catalog import WebDavCollectionEntry as Entry
from tgvio_player.infrastructure.webdav_read import WebDavRangeResponse


class BytesReader:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def open_range(self, package_path, relpath, byte_range):
        part = self.data[byte_range.start:byte_range.end + 1]

        async def body():
            yield part

        return WebDavRangeResponse(206, "video/mp4", len(part), None, None, body())


class Writer:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: list[str] = []

    async def ensure_directory(self, path: str) -> None:
        self.dirs.append(path)

    async def put_stream(self, path, chunks, *, size_bytes, content_type):
        data = b"".join([chunk async for chunk in chunks])
        self.files[path] = data
        return type("Receipt", (), {"size_bytes": len(data)})()


def package(path: str, media: tuple[CatalogMedia, ...], names: tuple[str, ...]) -> CatalogPackage:
    package_id = (path.encode().hex() + "0" * 64)[:64]
    return CatalogPackage(package_id, path, "a" * 64, None, None, media,
                          tuple(CatalogLocation(m.media_id, package_id, n) for m, n in zip(media, names)))


class RenditionScanAndStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = PlayerCatalogRepositorySQLite(Path(self.tmp.name) / "player.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_copies_are_scanned_and_linked_but_orphans_are_left_out(self) -> None:
        parent = plain_media_id(900, "Movie.mp4")
        dav_entries = {
            "长视频": (Entry("Movie.mp4", False, None, 900), Entry(".tgvio", True)),
            "长视频/.tgvio": (Entry(parent, True), Entry("f" * 64, True), Entry("notes", True)),
            f"长视频/.tgvio/{parent}": (Entry("480.mp4", False, None, 90), Entry("tmp.part", False, None, 5),
                                       Entry("cover.jpg", False, None, 12)),
            f"长视频/.tgvio/{'f' * 64}": (Entry("480.mp4", False, None, 80),),
        }

        class Dav:
            async def list_collection(self, path):
                return dav_entries[path]

            async def get_json(self, path, *, max_bytes):
                return None

        found = await LibraryCatalogSource(Dav(), roots=("长视频",)).discover()
        self.assertTrue(found.complete_scan)
        copies = [p for p in found.packages if "/.tgvio/" in p.remote_path]
        self.assertEqual(len(copies), 2)
        linked = next(p for p in copies if parent in p.remote_path)
        self.assertEqual([(m.media_id, m.variant_of, m.height) for m in linked.media],
                         [(rendition_media_id(parent, 480), parent, 480)])
        self.assertEqual([(c.media_id, c.remote_relpath) for c in linked.covers], [(parent, "cover.jpg")])

        for item in found.packages:
            await self.repo.apply_package(item)
        variants = await self.repo.active_variants(parent)
        self.assertEqual([v["height"] for v in variants], [480])
        self.assertIsNone(await self.repo.active_media_details(rendition_media_id("f" * 64, 480)),
                          "a copy of a missing original is not a video of its own")

    async def test_candidates_are_plain_videos_above_480p_without_a_copy(self) -> None:
        tall = CatalogMedia("b" * 64, "video", 900, "video/mp4", height=1080, duration_seconds=60.0)
        small = CatalogMedia("c" * 64, "video", 900, "video/mp4", height=360, duration_seconds=60.0)
        archived = CatalogMedia("d" * 64, "video", 900, "video/mp4", height=1080, duration_seconds=60.0)
        await self.repo.apply_package(package("长视频", (tall, small), ("a.mp4", "b.mp4")))
        await self.repo.apply_package(package("短视频/2026-09-22/1", (archived,), ("c.mp4",)))
        await self.repo.refresh_media_activity()
        self.assertEqual([row["media_id"] for row in await self.repo.list_rendition_candidates(limit=10)],
                         ["b" * 64])
        self.assertEqual(sorted(row["media_id"] for row in await self.repo.list_cover_candidates(limit=10)),
                         ["b" * 64, "c" * 64], "archive packages keep their own covers")


class SourceServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ranges_are_served_only_for_registered_tokens(self) -> None:
        server = _SourceServer(BytesReader(bytes(range(256)) * 4))
        await server.start()
        try:
            url = server.register("p", "f.mp4", 1024)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"Range": "bytes=10-19"}) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers["Content-Range"], "bytes 10-19/1024")
                    self.assertEqual(await response.read(), bytes(range(10, 20)))
                async with session.get(url, headers={"Range": "bytes=1000-"}) as response:
                    self.assertEqual(len(await response.read()), 24)
                async with session.get(url.rsplit("/", 1)[0] + "/guess") as response:
                    self.assertEqual(response.status, 404)
        finally:
            await server.close()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is not installed")
class RenditionMakerEncodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_720p_original_becomes_an_uploaded_480p_copy(self) -> None:
        with TemporaryDirectory() as tmp:
            original = Path(tmp) / "in.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(original)], check=True)
            data = original.read_bytes()
            writer = Writer()

            async def libraries():
                return ("长视频",)

            maker = RenditionMaker(None, BytesReader(data), work_dir=Path(tmp) / "work",
                                   writer=lambda: writer, libraries=libraries, is_playing=lambda: False)
            maker._work_dir.mkdir()
            await maker._source.start()
            try:
                made = await maker.make({"media_id": "e" * 64, "remote_path": "长视频/Series", "remote_relpath": "in.mp4",
                                         "size_bytes": len(data), "duration_seconds": 3.0}, asyncio.Event())
            finally:
                await maker._source.close()
            self.assertTrue(made)
            uploaded = writer.files[f"长视频/.tgvio/{'e' * 64}/480.mp4"]
            out = Path(tmp) / "out.mp4"
            out.write_bytes(uploaded)
            height = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                     "stream=height", "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
            self.assertEqual(height.strip(), "480")
            self.assertFalse(list((Path(tmp) / "work").iterdir()), "the local copy is removed after upload")

            await maker._source.start()
            try:
                self.assertTrue(await maker.make_cover({"media_id": "e" * 64, "remote_path": "长视频/Series",
                                                        "remote_relpath": "in.mp4", "size_bytes": len(data),
                                                        "duration_seconds": 3.0}))
            finally:
                await maker._source.close()
            self.assertEqual(writer.files[f"长视频/.tgvio/{'e' * 64}/cover.jpg"][:3], b"\xff\xd8\xff", "a JPEG")


if __name__ == "__main__":
    unittest.main()
