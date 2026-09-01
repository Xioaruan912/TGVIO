import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl import types

from src.media import FileTooLargeError, MediaDownloader, MediaPublisher, PublishPartialError
from src.services.splitter import create_split_bundle
from src.models import Job
from src.services.dedup import ContentHash
from tests.fakes import FakeClient, FakeStatusMessage


class MediaPublisherBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.client = FakeClient()
        self.publisher = MediaPublisher(
            self.client,
            "@destination",
            lambda _seq: self.tempdir.name,
            upload_timeout=30,
            max_file_size=1024 * 1024,
            forward_caption=True,
            cover_mode=True,
            channel_at="@channel",
            group_at="@group",
        )

    def test_telegram_document_filename_cannot_escape_job_directory(self) -> None:
        downloader = MediaDownloader(
            self.client,
            lambda _seq: self.tempdir.name,
            download_timeout=30,
        )
        media = SimpleNamespace(
            document=SimpleNamespace(
                attributes=[
                    types.DocumentAttributeFilename(file_name="../../.env")
                ],
                mime_type="video/mp4",
            )
        )
        self.assertEqual(downloader._media_filename(media), "env")

    def make_file(self, name: str, content: bytes = b"media") -> str:
        path = os.path.join(self.tempdir.name, name)
        with open(path, "wb") as media_file:
            media_file.write(content)
        return path

    def test_split_bundle_is_streamed_bounded_and_verifiable(self) -> None:
        source = self.make_file("very large.bin", b"abcdefghijk")
        bundle = create_split_bundle(source, self.tempdir.name, 4)
        self.assertEqual(len(bundle.parts), 3)
        self.assertTrue(all(os.path.getsize(part) <= 4 for part in bundle.parts))
        with open(bundle.manifest_path, encoding="utf-8") as manifest_file:
            manifest = __import__("json").load(manifest_file)
        self.assertEqual(manifest["original_size_bytes"], 11)
        self.assertEqual(manifest["part_count"], 3)
        self.assertEqual(manifest["mode"], "binary_volumes")
        from pathlib import Path
        self.assertEqual(b"".join(Path(part).read_bytes() for part in bundle.parts), b"abcdefghijk")

    async def test_oversize_split_publishes_manifest_then_ordered_parts_with_checkpoints(self) -> None:
        self.publisher.max_file_size = 8
        self.publisher.large_file_policy = "split"
        self.publisher.split_part_bytes = 5
        self.publisher.cover_mode = True
        path = self.make_file("large.bin", b"0123456789AB")
        job = Job(seq=77, kind="media", status=FakeStatusMessage(), message=SimpleNamespace(message="caption"))
        self.publisher._get_dest_input = AsyncMock(return_value="dest-input")
        self.publisher._upload_media_input = AsyncMock(side_effect=lambda path, *_args, **_kwargs: os.path.basename(path))
        checkpoints = []

        async def capture(_job, refs):
            checkpoints.append(refs)

        self.publisher.checkpoint_hooks.append(capture)
        result = await self.publisher._publish(job, path)

        self.assertEqual(len(result), 4)  # manifest + 3 bounded volumes
        self.assertEqual(len(checkpoints), 4)
        self.assertIn("parts.json", self.client.sent_files[0]["file"])
        self.assertTrue(all("part" in sent["file"] for sent in self.client.sent_files[1:]))
        self.assertIn("可校验分卷", self.client.sent_files[0]["caption"])

    def test_video_split_outputs_independently_probeable_mp4_segments(self) -> None:
        source = os.path.join(self.tempdir.name, "playable.mp4")
        generated = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
             "-t", "4", "-c:v", "libx264", "-b:v", "800k", "-g", "10", "-pix_fmt", "yuv420p", source],
            check=False,
        )
        self.assertEqual(generated.returncode, 0)
        bundle = create_split_bundle(source, self.tempdir.name, 180_000)
        self.assertEqual(bundle.mode, "playable_video_segments")
        self.assertGreaterEqual(len(bundle.parts), 2)
        for part in bundle.parts:
            self.assertLessEqual(os.path.getsize(part), 180_000)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", part],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(probe.returncode, 0)
            self.assertTrue(probe.stdout.strip())

    async def test_oversize_remains_rejected_without_opt_in(self) -> None:
        self.publisher.max_file_size = 8
        path = self.make_file("large.mp4", b"0123456789")
        with self.assertRaises(FileTooLargeError):
            await self.publisher._publish(Job(seq=78, kind="media", status=FakeStatusMessage()), path)

    async def test_dedup_hit_skips_byte_upload_and_preserves_new_caption(self) -> None:
        path = self.make_file("reuse.mp4", b"x" * 512)
        job = Job(
            seq=9,
            kind="media",
            status=FakeStatusMessage(),
            message=SimpleNamespace(message="新的文案"),
            spoiler=True,
        )
        content = ContentHash(os.path.realpath(path), "ab" * 32, 512)
        job._content_hashes = {os.path.realpath(path): content}
        entry = SimpleNamespace(id=7)
        manager = SimpleNamespace(
            reuse_input_media=AsyncMock(return_value=("reused-media", entry)),
            record_sent_message=AsyncMock(),
            mark_hit=AsyncMock(),
        )
        self.publisher.dedup_manager = manager
        self.publisher.cover_mode = False
        self.publisher._upload_media_input = AsyncMock(side_effect=AssertionError("must not upload bytes"))
        self.publisher._get_dest_input = AsyncMock(return_value="dest-input")

        result = await self.publisher._publish_media(job, path)

        self.assertEqual(len(result), 1)
        self.assertEqual(self.client.sent_files[0]["file"], "reused-media")
        self.assertIn("新的文案", self.client.sent_files[0]["caption"])
        self.publisher._upload_media_input.assert_not_awaited()
        manager.reuse_input_media.assert_awaited_once_with(
            self.client, content, spoiler=True, destination_key=None
        )
        manager.mark_hit.assert_awaited_once_with(entry, content)
        manager.record_sent_message.assert_awaited_once()

    async def test_dedup_miss_falls_back_to_normal_upload(self) -> None:
        path = self.make_file("fallback.mp4", b"y" * 512)
        job = Job(seq=8, kind="url", status=FakeStatusMessage())
        content = ContentHash(os.path.realpath(path), "cd" * 32, 512)
        job._content_hashes = {os.path.realpath(path): content}
        self.publisher.dedup_manager = SimpleNamespace(
            reuse_input_media=AsyncMock(return_value=(None, None)),
            record_sent_message=AsyncMock(),
            mark_hit=AsyncMock(),
        )
        self.publisher.cover_mode = False
        self.publisher._upload_media_input = AsyncMock(return_value="uploaded-media")
        self.publisher._get_dest_input = AsyncMock(return_value="dest-input")

        await self.publisher._publish_media(job, path)

        self.publisher._upload_media_input.assert_awaited_once()
        self.assertEqual(self.client.sent_files[0]["file"], "uploaded-media")

    async def test_collection_cover_preserves_text_order_and_returns_peer_pairs(self) -> None:
        photo = self.make_file("cover.jpg")
        video = self.make_file("video.mp4")
        job = Job(
            seq=10,
            kind="collection",
            status=FakeStatusMessage(),
            album=[
                SimpleNamespace(message="原图文案"),
                SimpleNamespace(message="原视频文案"),
            ],
            spoiler=True,
            user_id=42,
            texts=[" 第一行\n\n第二行 ", "第三行"],
        )
        self.publisher._get_dest_input = AsyncMock(return_value="dest-input")
        self.publisher._send_album_media = AsyncMock(return_value=[101])
        self.publisher._upload_media_input = AsyncMock(return_value="video-input")
        self.publisher._post_comment = AsyncMock(
            return_value=("discussion-input", 202)
        )

        with patch("src.media.is_photo_path", side_effect=lambda p: p.endswith(".jpg")):
            result = await self.publisher._publish_collection(job, [photo, video])

        self.assertEqual(
            result,
            [("dest-input", 101), ("discussion-input", 202)],
        )
        send_album = self.publisher._send_album_media.await_args
        self.assertEqual(
            send_album.kwargs["forced_captions"],
            ["第一行\n第二行\n第三行\n原图文案"],
        )
        self.assertEqual(send_album.kwargs["role"], "cover")
        self.publisher._upload_media_input.assert_awaited_once_with(
            video, True, 10, item=1, items=1
        )
        self.publisher._post_comment.assert_awaited_once_with(job, "video-input", 101)

    async def test_ordered_photo_collection_splits_media_groups_at_ten(self) -> None:
        paths = [self.make_file(f"photo-{index}.jpg") for index in range(23)]
        captions = [f"caption-{index}" for index in range(23)]
        job = Job(
            seq=20,
            kind="collection",
            status=FakeStatusMessage(),
            spoiler=False,
        )
        calls: list[tuple[list[str], list[str]]] = []

        async def fake_send_album(
            _job, chunk_paths, _dest_input, _spoiler, forced_captions=None, **_kwargs
        ):
            calls.append((list(chunk_paths), list(forced_captions or [])))
            start = sum(len(previous[0]) for previous in calls[:-1])
            return list(range(start + 1, start + len(chunk_paths) + 1))

        self.publisher._send_album_media = fake_send_album
        refs = await self.publisher._publish_ordered(
            job, paths, "dest-input", captions
        )

        self.assertEqual([len(chunk) for chunk, _ in calls], [10, 10, 3])
        self.assertEqual([caps for _, caps in calls], [captions[:10], captions[10:20], captions[20:]])
        self.assertEqual(refs, list(range(1, 24)))

    async def test_single_cover_publish_returns_channel_and_comment_references(self) -> None:
        video = self.make_file("single.mp4")
        cover = self.make_file("single-cover.jpg")
        job = Job(seq=11, kind="media", status=FakeStatusMessage())
        self.publisher._upload_media_input = AsyncMock(return_value="cover-input")
        self.publisher._get_dest_input = AsyncMock(return_value="dest-input")
        self.publisher._post_comment = AsyncMock(
            return_value=("discussion-input", 303)
        )

        with patch("src.media.make_cover", new=AsyncMock(return_value=cover)):
            result = await self.publisher._publish_cover_video(
                job, video, "video-input", "caption"
            )

        cover_message = self.client.sent_files[0]["message"]
        self.assertEqual(
            result,
            [("dest-input", cover_message.id), ("discussion-input", 303)],
        )
        self.assertEqual(self.client.sent_files[0]["caption"], "caption")
        self.publisher._post_comment.assert_awaited_once_with(
            job, "video-input", cover_message
        )

    async def test_confirmed_side_effects_are_checkpointed_before_completion(self) -> None:
        video = self.make_file("checkpoint.mp4")
        cover = self.make_file("checkpoint-cover.jpg")
        job = Job(
            seq=12,
            kind="media",
            status=FakeStatusMessage(),
            message=SimpleNamespace(message=""),
        )
        checkpoints = []

        async def capture(_job, refs) -> None:
            checkpoints.append(list(refs))

        self.publisher.checkpoint_hooks.append(capture)
        self.publisher._upload_media_input = AsyncMock(return_value="input")
        self.publisher._get_dest_input = AsyncMock(return_value=1234)
        self.publisher._post_comment = AsyncMock(return_value=(5678, 303))
        with patch("src.media.make_cover", new=AsyncMock(return_value=cover)):
            result = await self.publisher.publish(job, video)

        cover_id = self.client.sent_files[0]["message"].id
        self.assertEqual(result, [(1234, cover_id), (5678, 303)])
        self.assertEqual(
            checkpoints,
            [[(1234, cover_id, "cover")], [(5678, 303, "comment")]],
        )

    async def test_send_started_then_failed_is_partial_and_never_falls_back(self) -> None:
        video = self.make_file("partial.mp4")
        cover = self.make_file("partial-cover.jpg")
        job = Job(
            seq=13,
            kind="media",
            status=FakeStatusMessage(),
            message=SimpleNamespace(message=""),
        )
        self.publisher._upload_media_input = AsyncMock(return_value="input")
        self.publisher._get_dest_input = AsyncMock(return_value=1234)

        async def uncertain_comment(target_job, _media, _root):
            self.publisher._begin_send(target_job)
            raise TimeoutError("secret upstream")

        self.publisher._post_comment = uncertain_comment
        with patch("src.media.make_cover", new=AsyncMock(return_value=cover)):
            with self.assertRaises(PublishPartialError):
                await self.publisher.publish(job, video)

        self.assertEqual(len(self.client.sent_files), 1)
        self.assertEqual(len(job._published_refs), 1)

    async def test_thread_root_is_found_by_channel_post_and_then_cached(self) -> None:
        mirrored = SimpleNamespace(
            id=55,
            fwd_from=SimpleNamespace(channel_post=777),
        )
        self.publisher._group_max_id = 50
        self.publisher._get_discussion_group = AsyncMock(return_value="group-input")
        self.publisher._scan_group = AsyncMock(return_value=[mirrored])

        with patch("src.media.asyncio.sleep", new=AsyncMock()):
            first = await self.publisher._find_thread_root(777)
            second = await self.publisher._find_thread_root(777)

        self.assertEqual((first, second), (55, 55))
        self.assertEqual(self.publisher._group_max_id, 55)
        self.publisher._scan_group.assert_awaited_once_with(40, 450)

    def test_collection_text_join_and_footer_are_bounded(self) -> None:
        job = SimpleNamespace(texts=[" a \n\n b", " c "])
        self.assertEqual(
            self.publisher._join_collection_texts(job),
            "a\nb\nc",
        )
        rendered = self.publisher._with_footer("x" * 2000)
        self.assertLessEqual(len(rendered), 1024)
        self.assertTrue(rendered.endswith("@channel @group"))


if __name__ == "__main__":
    unittest.main()
