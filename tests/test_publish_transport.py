from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from telethon.tl import functions, types

from tgvio.adapters.telegram.discussion_resolver import DiscussionRoot
from tgvio.application.ports import (
    PublishTransportPartialError,
    PublishTransportUncertainError,
)
from tgvio.adapters.telegram.publish_transport import (
    TelethonPublishTransport,
    UnsupportedPublishStep,
)
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind
from tgvio.domain.publish import (
    PublishEffect,
    PublishStep,
    PublishStepKind,
    PublishTarget,
)
from tgvio.infrastructure.media_transformer import FFmpegMediaTransformer, SplitBundle


class FakeTransformer:
    def __init__(self) -> None:
        self.cover_calls: list[int] = []
        self.remux_calls: list[int] = []
        self.thumbnail_calls: list[int] = []
        self.thumbnail_enabled = False
        self.playable_split_calls: list[int] = []
        self.binary_split_calls: list[int] = []

    async def make_video_cover(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
        duration_seconds: float | None,
        max_width: int,
    ) -> Path:
        self.cover_calls.append(item_index)
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / f"cover-{item_index}.jpg"
        out.write_bytes(b"cover")
        return out

    async def remux_faststart(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
    ) -> Path:
        self.remux_calls.append(item_index)
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / f"faststart-{item_index}.mp4"
        out.write_bytes(source.read_bytes())
        return out

    async def make_video_thumbnail(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
        duration_seconds: float | None,
        max_size: int = 320,
        max_bytes: int = 40 * 1024,
    ) -> Path | None:
        self.thumbnail_calls.append(item_index)
        if not self.thumbnail_enabled:
            return None
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / f"thumb-{item_index}.jpg"
        out.write_bytes(b"thumb")
        return out

    async def make_playable_segments(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
        part_bytes: int,
        duration_seconds: float | None,
    ) -> SplitBundle:
        self.playable_split_calls.append(item_index)
        target_dir.mkdir(parents=True, exist_ok=True)
        manifest = target_dir / f"video-{item_index}.parts.json"
        manifest.write_text("{}\n", encoding="utf-8")
        parts = tuple(target_dir / f"video-{item_index}-{part}.mp4" for part in (1, 2))
        for path in parts:
            path.write_bytes(b"segment")
        return SplitBundle(
            mode="playable_video_segments",
            manifest_path=manifest,
            parts=parts,
            original_name=source.name,
            original_sha256="video-hash",
        )

    async def make_binary_volumes(
        self,
        source: Path,
        target_dir: Path,
        *,
        item_index: int,
        part_bytes: int,
    ) -> SplitBundle:
        self.binary_split_calls.append(item_index)
        target_dir.mkdir(parents=True, exist_ok=True)
        manifest = target_dir / f"file-{item_index}.parts.json"
        manifest.write_text("{}\n", encoding="utf-8")
        parts = tuple(target_dir / f"file-{item_index}.part{part:03d}" for part in (1, 2, 3))
        for path in parts:
            path.write_bytes(b"part")
        return SplitBundle(
            mode="binary_volumes",
            manifest_path=manifest,
            parts=parts,
            original_name=source.name,
            original_sha256="file-hash",
        )


class FakeTelegramClient:
    def __init__(self) -> None:
        self.send_calls: list[tuple[object, object, dict]] = []
        self.requests: list[object] = []
        self.fail_send_on: int | None = None
        self.fail_album_send = False
        self.reuse_message = None
        self.fail_upload_part_once = False
        self._upload_part_failed = False
        self.upload_calls: list[str] = []

    async def get_input_entity(self, entity):
        return entity

    async def get_messages(self, entity, ids):
        return self.reuse_message

    async def upload_file(self, file):
        self.upload_calls.append(str(file))
        return types.InputFile(
            id=123456,
            parts=1,
            name=Path(file).name,
            md5_checksum="",
        )

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(
            request,
            (
                functions.upload.SaveFilePartRequest,
                functions.upload.SaveBigFilePartRequest,
            ),
        ):
            if self.fail_upload_part_once and not self._upload_part_failed:
                self._upload_part_failed = True
                raise RuntimeError("fixture-part-upload-failure")
            return True
        if isinstance(request, functions.messages.GetDiscussionMessageRequest):
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(
                        id=700,
                        peer_id=types.PeerChannel(channel_id=222),
                    )
                ]
            )
        if isinstance(request, functions.messages.UploadMediaRequest):
            if isinstance(request.media, types.InputMediaUploadedPhoto):
                return types.MessageMediaPhoto(
                    photo=types.Photo(
                        id=1000 + len(self.requests),
                        access_hash=2000 + len(self.requests),
                        file_reference=b"photo-ref",
                        date=None,
                        sizes=[],
                        dc_id=4,
                    )
                )
            if isinstance(request.media, types.InputMediaUploadedDocument):
                return types.MessageMediaDocument(
                    document=types.Document(
                        id=3000 + len(self.requests),
                        access_hash=4000 + len(self.requests),
                        file_reference=b"document-ref",
                        date=None,
                        mime_type=request.media.mime_type,
                        size=7,
                        dc_id=4,
                        attributes=request.media.attributes,
                    )
                )
        if isinstance(request, functions.messages.SendMultiMediaRequest):
            if self.fail_album_send:
                raise RuntimeError("album-send-response-lost")
            channel_id = 111 if request.peer == "@channel" else 222
            return SimpleNamespace(
                updates=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            id=800 + index,
                            peer_id=types.PeerChannel(channel_id=channel_id),
                        )
                    )
                    for index, _media in enumerate(request.multi_media)
                ]
            )
        raise AssertionError(f"unexpected request: {request!r}")

    async def send_file(self, entity, file, **kwargs):
        call_number = len(self.send_calls) + 1
        if self.fail_send_on == call_number:
            raise RuntimeError(f"send-failed-{call_number}")
        self.send_calls.append((entity, file, kwargs))
        count = len(file) if isinstance(file, list) else 1
        channel_id = 111 if entity == "@channel" else 222
        messages = [
            SimpleNamespace(
                id=900 + len(self.send_calls) * 10 + index,
                peer_id=types.PeerChannel(channel_id=channel_id),
            )
            for index in range(count)
        ]
        return messages if count > 1 else messages[0]


class FakeDiscussionResolver:
    def __init__(self, root: DiscussionRoot | None) -> None:
        self.root = root
        self.calls: list[tuple[str | int, tuple[int, ...]]] = []

    async def resolve(self, channel_chat_id, channel_message_ids):
        self.calls.append((channel_chat_id, tuple(channel_message_ids)))
        return self.root


class TelethonPublishTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = FakeTelegramClient()
        self.transformer = FakeTransformer()
        self.transport = TelethonPublishTransport(
            self.client,
            self.transformer,
            self.root / "work",
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _item(
        self,
        index: int,
        kind: MediaKind,
        *,
        caption: str = "",
        spoiler: bool = False,
    ) -> MediaItem:
        suffix = ".jpg" if kind == MediaKind.PHOTO else ".mp4"
        path = self.root / f"item-{index}{suffix}"
        path.write_bytes(b"payload")
        return MediaItem(
            index=index,
            kind=kind,
            source=f"fixture:{index}",
            local_path=str(path),
            caption=caption,
            spoiler=spoiler,
            duration_seconds=10.0 if kind == MediaKind.VIDEO else None,
        )

    async def test_channel_album_sends_native_files_and_returns_receipts(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.PHOTO, caption="a"), self._item(1, MediaKind.PHOTO, caption="b")],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_COVER_ALBUM,
            target=PublishTarget.CHANNEL,
            item_indexes=(0, 1),
            params={"forward_caption": True, "strategies": {"0": "native", "1": "native"}},
        )
        receipts = await self.transport.execute_step(job, step, ())
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(receipt.effect_type == "telegram_channel_message" for receipt in receipts))
        uploads = [
            request
            for request in self.client.requests
            if isinstance(request, functions.messages.UploadMediaRequest)
        ]
        sends = [
            request
            for request in self.client.requests
            if isinstance(request, functions.messages.SendMultiMediaRequest)
        ]
        self.assertEqual(len(uploads), 2)
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].peer, "@channel")
        self.assertEqual([media.message for media in sends[0].multi_media], ["a", "b"])
        self.assertIsNone(sends[0].reply_to)
        self.assertEqual(self.client.send_calls, [])

    async def test_single_visible_send_failure_is_uncertain(self) -> None:
        self.client.fail_send_on = 1
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.PHOTO)],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "native"}},
        )
        with self.assertRaises(PublishTransportUncertainError):
            await self.transport.execute_step(job, step, ())

    async def test_album_visible_send_failure_is_uncertain(self) -> None:
        self.client.fail_album_send = True
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.PHOTO), self._item(1, MediaKind.PHOTO)],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_COVER_ALBUM,
            target=PublishTarget.CHANNEL,
            item_indexes=(0, 1),
            params={"strategies": {"0": "native", "1": "native"}},
        )
        with self.assertRaises(PublishTransportUncertainError):
            await self.transport.execute_step(job, step, ())

    async def test_album_spoiler_survives_upload_media_conversion(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[
                self._item(0, MediaKind.PHOTO, spoiler=True),
                self._item(1, MediaKind.PHOTO, spoiler=True),
            ],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_COVER_ALBUM,
            target=PublishTarget.CHANNEL,
            item_indexes=(0, 1),
            params={"strategies": {"0": "native", "1": "native"}},
        )
        await self.transport.execute_step(job, step, ())
        send = next(
            request
            for request in self.client.requests
            if isinstance(request, functions.messages.SendMultiMediaRequest)
        )
        self.assertTrue(all(media.media.spoiler for media in send.multi_media))

    async def test_album_reuse_reference_bypasses_upload_media_request(self) -> None:
        original = self._item(0, MediaKind.PHOTO)
        reused = MediaItem(
            **{
                field: getattr(original, field)
                for field in original.__dataclass_fields__
                if field != "telegram_ref"
            },
            telegram_ref="telegram:-100111:500",
        )
        self.client.reuse_message = SimpleNamespace(
            photo=types.Photo(
                id=123,
                access_hash=456,
                file_reference=b"reuse-ref",
                date=None,
                sizes=[],
                dc_id=4,
            ),
            document=None,
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[reused, self._item(1, MediaKind.PHOTO)],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_COVER_ALBUM,
            target=PublishTarget.CHANNEL,
            item_indexes=(0, 1),
            params={"strategies": {"0": "reuse_reference", "1": "native"}},
        )
        await self.transport.execute_step(job, step, ())
        uploads = [
            request
            for request in self.client.requests
            if isinstance(request, functions.messages.UploadMediaRequest)
        ]
        self.assertEqual(len(uploads), 1)
        send = next(
            request
            for request in self.client.requests
            if isinstance(request, functions.messages.SendMultiMediaRequest)
        )
        self.assertIsInstance(send.multi_media[0].media, types.InputMediaPhoto)

    async def test_configured_bounded_uploader_handles_video_before_visible_send(self) -> None:
        item = self._item(0, MediaKind.VIDEO)
        Path(item.local_path).write_bytes(b"x" * (3 * 64 * 1024))
        transport = TelethonPublishTransport(
            self.client,
            self.transformer,
            self.root / "work-concurrent-upload",
            upload_workers=4,
            upload_global_workers=4,
            upload_part_size_kb=64,
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[item],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "native"}},
        )

        await transport.execute_step(job, step, ())

        self.assertTrue(
            any(isinstance(request, functions.upload.SaveFilePartRequest) for request in self.client.requests)
        )
        _entity, file_arg, _kwargs = self.client.send_calls[0]
        self.assertIsInstance(file_arg, types.InputMediaUploadedDocument)

    async def test_part_failure_falls_back_before_exactly_one_visible_send(self) -> None:
        item = self._item(0, MediaKind.VIDEO)
        Path(item.local_path).write_bytes(b"x" * (3 * 64 * 1024))
        self.client.fail_upload_part_once = True
        transport = TelethonPublishTransport(
            self.client,
            self.transformer,
            self.root / "work-upload-fallback",
            upload_workers=4,
            upload_global_workers=4,
            upload_part_size_kb=64,
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[item],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "native"}},
        )

        receipts = await transport.execute_step(job, step, ())

        self.assertEqual(len(self.client.upload_calls), 1)
        self.assertEqual(len(self.client.send_calls), 1)
        self.assertEqual(len(receipts), 1)

    async def test_video_album_upload_carries_generated_thumbnail(self) -> None:
        self.transformer.thumbnail_enabled = True
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[
                self._item(0, MediaKind.VIDEO),
                self._item(1, MediaKind.VIDEO),
            ],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0, 1),
            params={"strategies": {"0": "native", "1": "native"}},
        )
        await self.transport.execute_step(job, step, ())
        uploads = [
            request.media
            for request in self.client.requests
            if isinstance(request, functions.messages.UploadMediaRequest)
        ]
        self.assertEqual(self.transformer.thumbnail_calls, [0, 1])
        self.assertEqual(len(uploads), 2)
        self.assertTrue(all(isinstance(media, types.InputMediaUploadedDocument) for media in uploads))
        self.assertTrue(all(media.thumb is not None for media in uploads))

    async def test_discussion_step_resolves_thread_from_confirmed_channel_effect(self) -> None:
        resolver = FakeDiscussionResolver(
            DiscussionRoot(chat_id=-100222, message_id=700, source_message_id=500)
        )
        transport = TelethonPublishTransport(
            self.client,
            self.transformer,
            self.root / "work",
            discussion_resolver=resolver,
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.VIDEO, caption="video")],
        )
        step = PublishStep(
            index=1,
            kind=PublishStepKind.DISCUSSION_VIDEO_ALBUM,
            target=PublishTarget.DISCUSSION,
            item_indexes=(0,),
            params={"forward_caption": True, "strategies": {"0": "native"}},
        )
        prior = (
            PublishEffect(
                plan_id="plan",
                step_index=0,
                effect_type="telegram_channel_message",
                external_chat_id="-1000000000111",
                external_message_id="500",
            ),
        )
        receipts = await transport.execute_step(job, step, prior)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(resolver.calls, [("-1000000000111", (500,))])
        self.assertFalse(
            any(
                isinstance(request, functions.messages.GetDiscussionMessageRequest)
                for request in self.client.requests
            )
        )
        entity, _file, kwargs = self.client.send_calls[0]
        self.assertEqual(entity, -100222)
        self.assertEqual(kwargs["reply_to"], 700)
        self.assertEqual(receipts[0].effect_type, "telegram_discussion_message")

    async def test_channel_cover_persists_bot_safe_discussion_root_mapping(self) -> None:
        resolver = FakeDiscussionResolver(
            DiscussionRoot(chat_id=-100222, message_id=701, source_message_id=801)
        )
        transport = TelethonPublishTransport(
            self.client,
            self.transformer,
            self.root / "work",
            discussion_resolver=resolver,
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.PHOTO), self._item(1, MediaKind.PHOTO)],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_COVER_ALBUM,
            target=PublishTarget.CHANNEL,
            item_indexes=(0, 1),
            params={"strategies": {"0": "native", "1": "native"}},
        )
        receipts = await transport.execute_step(job, step, ())
        self.assertEqual(resolver.calls, [("-1000000000111", (800, 801))])
        mapped = next(
            receipt for receipt in receipts if receipt.external_message_id == "801"
        )
        self.assertEqual(mapped.detail["discussion_chat_id"], -100222)
        self.assertEqual(mapped.detail["discussion_message_id"], 701)

    async def test_channel_root_steps_are_serialized_before_discussion_capture(self) -> None:
        gate = asyncio.Event()
        entered = 0
        max_entered = 0

        class BlockingResolver(FakeDiscussionResolver):
            async def resolve(self, channel_chat_id, channel_message_ids):
                nonlocal entered, max_entered
                entered += 1
                max_entered = max(max_entered, entered)
                if len(self.calls) == 0:
                    gate.set()
                    await asyncio.sleep(0.03)
                self.calls.append((channel_chat_id, tuple(channel_message_ids)))
                entered -= 1
                return DiscussionRoot(
                    chat_id=-100222,
                    message_id=700 + len(self.calls),
                    source_message_id=int(channel_message_ids[-1]),
                )

        resolver = BlockingResolver(None)
        transport = TelethonPublishTransport(
            self.client,
            self.transformer,
            self.root / "work",
            discussion_resolver=resolver,
        )
        jobs = [
            Job(
                owner_id=42,
                destination="@channel",
                state=JobState.PLANNED,
                items=[self._item(index, MediaKind.PHOTO)],
            )
            for index in (0, 1)
        ]
        steps = [
            PublishStep(
                index=0,
                kind=PublishStepKind.CHANNEL_COVER_ALBUM,
                target=PublishTarget.CHANNEL,
                item_indexes=(index,),
                params={"strategies": {str(index): "native"}},
            )
            for index in (0, 1)
        ]
        first = asyncio.create_task(transport.execute_step(jobs[0], steps[0], ()))
        await gate.wait()
        second = asyncio.create_task(transport.execute_step(jobs[1], steps[1], ()))
        await asyncio.gather(first, second)
        self.assertEqual(max_entered, 1)

    async def test_discussion_step_prefers_durable_root_mapping_without_lookup(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.VIDEO)],
        )
        step = PublishStep(
            index=1,
            kind=PublishStepKind.DISCUSSION_VIDEO_ALBUM,
            target=PublishTarget.DISCUSSION,
            item_indexes=(0,),
            params={"strategies": {"0": "native"}},
        )
        prior = (
            PublishEffect(
                plan_id="plan",
                step_index=0,
                effect_type="telegram_channel_message",
                external_chat_id="-1000000000111",
                external_message_id="500",
                detail={
                    "discussion_chat_id": -100222,
                    "discussion_message_id": 700,
                },
            ),
        )
        receipts = await self.transport.execute_step(job, step, prior)
        self.assertEqual(receipts[0].effect_type, "telegram_discussion_message")
        entity, _file, kwargs = self.client.send_calls[0]
        self.assertEqual(entity, -100222)
        self.assertEqual(kwargs["reply_to"], 700)

    async def test_faststart_strategy_uses_transform_before_send(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.VIDEO)],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"forward_caption": True, "strategies": {"0": "remux_faststart"}},
        )
        await self.transport.execute_step(job, step, ())
        self.assertEqual(self.transformer.remux_calls, [0])
        _entity, file_arg, _kwargs = self.client.send_calls[0]
        self.assertIn("faststart-0.mp4", str(file_arg))

    async def test_reuse_reference_sends_existing_photo_media(self) -> None:
        item = self._item(0, MediaKind.PHOTO)
        item = MediaItem(
            **{
                field: getattr(item, field)
                for field in item.__dataclass_fields__
                if field != "telegram_ref"
            },
            telegram_ref="telegram:-100111:500",
        )
        self.client.reuse_message = SimpleNamespace(
            photo=types.Photo(
                id=123,
                access_hash=456,
                file_reference=b"photo-ref",
                date=None,
                sizes=[],
                dc_id=4,
            ),
            document=None,
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[item],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "reuse_reference"}},
        )
        receipts = await self.transport.execute_step(job, step, ())
        _entity, file_arg, _kwargs = self.client.send_calls[0]
        self.assertIsInstance(file_arg, types.InputMediaPhoto)
        self.assertEqual(receipts[0].detail["reusable_ref"].split(":", 1)[0], "telegram")

    async def test_stale_reuse_reference_falls_back_to_local_upload(self) -> None:
        original = self._item(0, MediaKind.PHOTO)
        item = MediaItem(
            **{
                field: getattr(original, field)
                for field in original.__dataclass_fields__
                if field != "telegram_ref"
            },
            telegram_ref="telegram:-100111:999",
        )
        self.client.reuse_message = None
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[item],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "reuse_reference"}},
        )
        await self.transport.execute_step(job, step, ())
        _entity, file_arg, _kwargs = self.client.send_calls[0]
        self.assertEqual(str(file_arg), item.local_path)

    async def test_video_cover_is_generated_before_channel_send(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.VIDEO, caption="caption")],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_VIDEO_COVER,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"forward_caption": True, "strategies": {"0": "native"}},
        )
        await self.transport.execute_step(job, step, ())
        self.assertEqual(self.transformer.cover_calls, [0])
        _entity, file_arg, kwargs = self.client.send_calls[0]
        self.assertIn("cover-0.jpg", str(file_arg))
        self.assertEqual(kwargs["caption"], "caption")

    async def test_footer_is_kept_when_original_caption_forwarding_is_off(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.PHOTO, caption="should-not-forward")],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={
                "forward_caption": False,
                "caption_footer": "@channel @group",
                "strategies": {"0": "native"},
            },
        )
        await self.transport.execute_step(job, step, ())
        _entity, _file, kwargs = self.client.send_calls[0]
        self.assertEqual(kwargs["caption"], "@channel @group")

    def test_caption_footer_preserves_telegram_1024_limit(self) -> None:
        item = self._item(0, MediaKind.PHOTO, caption="x" * 2000)
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={
                "forward_caption": True,
                "caption_footer": "@channel @group",
                "strategies": {"0": "native"},
            },
        )
        caption = self.transport._caption(item, step)
        self.assertEqual(len(caption), 1024)
        self.assertTrue(caption.endswith("\n@channel @group"))

    def test_collection_caption_precedes_original_caption_and_footer_with_limit(self) -> None:
        item = self._item(0, MediaKind.PHOTO, caption="原文")
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_COVER_ALBUM,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={
                "forward_caption": True,
                "collection_caption": "合集文字第一行\n合集文字第二行",
                "collection_caption_item_index": 0,
                "caption_footer": "@channel @group",
                "strategies": {"0": "native"},
            },
        )
        caption = self.transport._caption(item, step)
        self.assertEqual(
            caption,
            "合集文字第一行\n合集文字第二行\n原文\n@channel @group",
        )

        long_step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_COVER_ALBUM,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={
                **step.params,
                "collection_caption": "文" * 2000,
            },
        )
        limited = self.transport._caption(item, long_step)
        self.assertEqual(len(limited), 1024)
        self.assertTrue(limited.endswith("\n@channel @group"))

    async def test_album_captions_receive_footer(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[
                self._item(0, MediaKind.PHOTO, caption="a"),
                self._item(1, MediaKind.PHOTO, caption="b"),
            ],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_COVER_ALBUM,
            target=PublishTarget.CHANNEL,
            item_indexes=(0, 1),
            params={
                "forward_caption": True,
                "caption_footer": "@channel @group",
                "strategies": {"0": "native", "1": "native"},
            },
        )
        await self.transport.execute_step(job, step, ())
        send = next(
            request
            for request in self.client.requests
            if isinstance(request, functions.messages.SendMultiMediaRequest)
        )
        self.assertEqual(
            [media.message for media in send.multi_media],
            ["a\n@channel @group", "b\n@channel @group"],
        )

    async def test_unknown_strategy_rejects_before_any_send(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.VIDEO)],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "mystery_strategy"}},
        )
        with self.assertRaisesRegex(UnsupportedPublishStep, "mystery_strategy"):
            await self.transport.execute_step(job, step, ())
        self.assertEqual(self.client.send_calls, [])

    async def test_playable_split_sends_manifest_then_streamable_segments(self) -> None:
        item = self._item(0, MediaKind.VIDEO, caption="original caption")
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[item],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"forward_caption": True, "strategies": {"0": "split_playable"}},
        )
        receipts = await self.transport.execute_step(job, step, ())
        self.assertEqual(self.transformer.playable_split_calls, [0])
        self.assertEqual(len(receipts), 3)
        self.assertEqual(len(self.client.send_calls), 3)
        _entity, manifest, manifest_kwargs = self.client.send_calls[0]
        self.assertIn(".parts.json", str(manifest))
        self.assertTrue(manifest_kwargs["force_document"])
        self.assertIn("original caption", manifest_kwargs["caption"])
        for _entity, part, kwargs in self.client.send_calls[1:]:
            self.assertIn(".mp4", str(part))
            self.assertFalse(kwargs["force_document"])
            self.assertTrue(kwargs["supports_streaming"])
        self.assertEqual(receipts[0].detail["split_role"], "manifest")
        self.assertEqual(receipts[1].detail["split_role"], "part")

    async def test_binary_split_sends_manifest_and_documents(self) -> None:
        path = self.root / "archive.bin"
        path.write_bytes(b"payload")
        item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="fixture:0",
            local_path=str(path),
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[item],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_DOCUMENT,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "binary_volume"}},
        )
        receipts = await self.transport.execute_step(job, step, ())
        self.assertEqual(self.transformer.binary_split_calls, [0])
        self.assertEqual(len(receipts), 4)
        self.assertEqual(len(self.client.send_calls), 4)
        self.assertTrue(all(call[2]["force_document"] for call in self.client.send_calls))

    async def test_split_partial_error_preserves_successful_receipts(self) -> None:
        path = self.root / "archive.bin"
        path.write_bytes(b"payload")
        item = MediaItem(
            index=0,
            kind=MediaKind.DOCUMENT,
            source="fixture:0",
            local_path=str(path),
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[item],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_DOCUMENT,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "binary_volume"}},
        )
        self.client.fail_send_on = 3
        with self.assertRaises(PublishTransportPartialError) as ctx:
            await self.transport.execute_step(job, step, ())
        self.assertEqual(len(ctx.exception.receipts), 2)
        self.assertEqual(len(self.client.send_calls), 2)
        self.assertEqual(ctx.exception.receipts[0].detail["split_role"], "manifest")
        self.assertEqual(ctx.exception.receipts[1].detail["part_index"], 1)

    async def test_spoiler_photo_uses_uploaded_input_media_with_spoiler(self) -> None:
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[self._item(0, MediaKind.PHOTO, spoiler=True)],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "native"}},
        )
        await self.transport.execute_step(job, step, ())
        self.assertEqual(len(self.client.send_calls), 1)
        _entity, file_arg, _kwargs = self.client.send_calls[0]
        self.assertIsInstance(file_arg, types.InputMediaUploadedPhoto)
        self.assertTrue(file_arg.spoiler)

    async def test_reused_photo_preserves_spoiler(self) -> None:
        original = self._item(0, MediaKind.PHOTO, spoiler=True)
        item = MediaItem(
            **{
                field: getattr(original, field)
                for field in original.__dataclass_fields__
                if field != "telegram_ref"
            },
            telegram_ref="telegram:-100111:500",
        )
        self.client.reuse_message = SimpleNamespace(
            photo=types.Photo(
                id=123,
                access_hash=456,
                file_reference=b"photo-ref",
                date=None,
                sizes=[],
                dc_id=4,
            ),
            document=None,
        )
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[item],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "reuse_reference"}},
        )
        await self.transport.execute_step(job, step, ())
        _entity, file_arg, _kwargs = self.client.send_calls[0]
        self.assertIsInstance(file_arg, types.InputMediaPhoto)
        self.assertTrue(file_arg.spoiler)

    async def test_playable_split_parts_preserve_spoiler_but_manifest_does_not(self) -> None:
        item = self._item(0, MediaKind.VIDEO, spoiler=True)
        job = Job(
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=[item],
        )
        step = PublishStep(
            index=0,
            kind=PublishStepKind.CHANNEL_MEDIA_GROUP,
            target=PublishTarget.CHANNEL,
            item_indexes=(0,),
            params={"strategies": {"0": "split_playable"}},
        )
        await self.transport.execute_step(job, step, ())
        _entity, manifest, manifest_kwargs = self.client.send_calls[0]
        self.assertIsInstance(manifest, str)
        self.assertTrue(manifest_kwargs["force_document"])
        for _entity, part, kwargs in self.client.send_calls[1:]:
            self.assertIsInstance(part, types.InputMediaUploadedDocument)
            self.assertTrue(part.spoiler)
            self.assertTrue(kwargs["supports_streaming"])


class FFmpegMediaTransformerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cover_and_faststart_remux_work_on_real_video(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=96x64:d=1",
                "-an",
                "-c:v",
                "libx264",
                "-y",
                str(source),
            )
            self.assertEqual(await proc.wait(), 0)
            transformer = FFmpegMediaTransformer(timeout=20)
            cover = await transformer.make_video_cover(
                source,
                root / "work",
                item_index=0,
                duration_seconds=1.0,
                max_width=320,
            )
            self.assertTrue(cover.is_file())
            self.assertGreater(cover.stat().st_size, 0)
            remuxed = await transformer.remux_faststart(
                source,
                root / "work",
                item_index=0,
            )
            self.assertTrue(remuxed.is_file())
            self.assertGreater(remuxed.stat().st_size, 0)
            thumbnail = await transformer.make_video_thumbnail(
                source,
                root / "work",
                item_index=0,
                duration_seconds=1.0,
            )
            self.assertIsNotNone(thumbnail)
            assert thumbnail is not None
            self.assertTrue(thumbnail.is_file())
            self.assertLessEqual(thumbnail.stat().st_size, 40 * 1024)
            probe = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(thumbnail),
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await probe.communicate()
            self.assertEqual(probe.returncode, 0)
            width, height = (int(value) for value in stdout.decode().strip().split("x"))
            self.assertLessEqual(width, 320)
            self.assertLessEqual(height, 320)

    async def test_binary_volumes_reassemble_exact_original(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "archive.bin"
            source.write_bytes(bytes(range(251)) * 1000)
            transformer = FFmpegMediaTransformer(timeout=20)
            bundle = await transformer.make_binary_volumes(
                source,
                root / "work",
                item_index=2,
                part_bytes=32768,
            )
            self.assertEqual(bundle.mode, "binary_volumes")
            self.assertGreater(len(bundle.parts), 1)
            self.assertTrue(all(part.stat().st_size <= 32768 for part in bundle.parts))
            rebuilt = b"".join(part.read_bytes() for part in bundle.parts)
            self.assertEqual(rebuilt, source.read_bytes())
            self.assertTrue(bundle.manifest_path.is_file())

    async def test_playable_video_segments_are_bounded_and_probeable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "long.mp4"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=25:duration=4",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-b:v",
                "600k",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(source),
            )
            self.assertEqual(await proc.wait(), 0)
            part_bytes = max(32768, source.stat().st_size // 2)
            transformer = FFmpegMediaTransformer(timeout=20, split_timeout=60)
            bundle = await transformer.make_playable_segments(
                source,
                root / "work",
                item_index=3,
                part_bytes=part_bytes,
                duration_seconds=4.0,
            )
            self.assertEqual(bundle.mode, "playable_video_segments")
            self.assertGreaterEqual(len(bundle.parts), 2)
            self.assertTrue(all(0 < part.stat().st_size <= part_bytes for part in bundle.parts))
            for part in bundle.parts:
                probe = await asyncio.create_subprocess_exec(
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "csv=p=0",
                    str(part),
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout, _stderr = await probe.communicate()
                self.assertEqual(probe.returncode, 0)
                self.assertTrue(stdout.strip())
