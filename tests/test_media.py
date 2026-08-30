import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.media import MediaPublisher
from src.models import Job
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

    def make_file(self, name: str, content: bytes = b"media") -> str:
        path = os.path.join(self.tempdir.name, name)
        with open(path, "wb") as media_file:
            media_file.write(content)
        return path

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
        self.publisher._upload_media_input.assert_awaited_once_with(
            video, True, 10, item=1, items=1
        )
        self.publisher._post_comment.assert_awaited_once_with("video-input", 101)

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
            "video-input", cover_message
        )

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
