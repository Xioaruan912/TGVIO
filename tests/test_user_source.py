from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tgvio.adapters.telegram.intake_source import IntakeSourceMixin
from tgvio.adapters.telegram.user_source import UserSourceReader
from tgvio.application.intake import IncomingMedia
from tgvio.application.media_router import RoutedMediaDownloader
from tgvio.domain.job import MediaKind


def _message(
    message_id: int,
    *,
    chat_id: int = -100555,
    kind: str = "video",
    grouped_id: int | None = None,
    caption: str = "",
    size: int = 100,
):
    fields = {
        "photo": None,
        "video": None,
        "document": None,
        "audio": None,
        "voice": None,
    }
    fields[kind] = object()
    return SimpleNamespace(
        id=message_id,
        chat_id=chat_id,
        grouped_id=grouped_id,
        message=caption,
        file=SimpleNamespace(name=f"file-{message_id}.bin", size=size, ext=".bin"),
        media=SimpleNamespace(spoiler=False),
        **fields,
    )


class FakeUserClient:
    def __init__(self, messages: dict[object, object] | None = None) -> None:
        self.messages = messages or {}
        self.entities: dict[object, object] = {}
        self.iterated: list[object] = []

    async def get_entity(self, value):
        if value not in self.entities:
            raise ValueError(f"unknown entity {value!r}")
        return self.entities[value]

    async def get_messages(self, entity, ids):
        return self.messages.get(ids)

    def iter_messages(self, entity, *, min_id=0, max_id=0, limit=None, search=None):
        self.iterated.append((entity, min_id, max_id))

        async def generator():
            ordered = sorted(
                (message for message in self.messages.values() if message is not None),
                key=lambda message: message.id,
                reverse=True,
            )
            emitted = 0
            for message in ordered:
                if min_id and message.id <= min_id:
                    continue
                if max_id and message.id > max_id:
                    continue
                if (
                    entity is not None
                    and getattr(message, "chat_id", None) is not None
                    and int(entity) != int(message.chat_id)
                ):
                    continue
                if limit is not None and emitted >= int(limit):
                    break
                emitted += 1
                yield message

        return generator()


class UserSourceReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_resolves_whitelist_and_skips_bad_entries(self) -> None:
        client = FakeUserClient()
        reader = UserSourceReader(client, allowed_chats=["@good", "bad"])
        # utils.get_peer_id needs a real peer object; emulate with a Peer-like namespace
        from telethon.tl import types

        client.entities["@good"] = types.Channel(
            id=111, title="c", photo=None, date=None, creator=None, left=None,
            broadcast=True, verified=None, megagroup=None, restricted=None,
            signatures=None, min=None, scam=None, has_link=None, has_geo=None,
            slowmode_enabled=None, access_hash=5, username="good", restriction_reason=None,
            admin_rights=None, banned_rights=None, default_banned_rights=None,
            participants_count=None,
        )
        resolved = await reader.prepare()
        self.assertEqual(resolved, 1)
        self.assertIn(-1000000000111, reader.allowed_ids)

    async def test_list_recent_media_merges_albums_newest_first(self) -> None:
        client = FakeUserClient(
            {
                40: _message(40, kind="video"),
                39: _message(39, kind="photo", grouped_id=99, size=50),
                38: _message(38, kind="video", grouped_id=99, size=70),
                37: _message(37, kind="photo"),
            }
        )
        reader = UserSourceReader(client)
        summaries, has_more = await reader.list_recent_media(-100555, limit=10)
        self.assertFalse(has_more)
        self.assertEqual([item.message_id for item in summaries], [40, 39, 37])
        album = summaries[1]
        self.assertEqual(album.item_count, 2)
        self.assertEqual(album.size_bytes, 120)
        self.assertEqual(album.kinds, (MediaKind.PHOTO, MediaKind.VIDEO))
        self.assertFalse(album.is_photo_only)
        self.assertTrue(album.has_video)

    async def test_list_recent_media_pages_with_has_more(self) -> None:
        client = FakeUserClient(
            {
                40: _message(40, kind="video"),
                39: _message(39, kind="video"),
                38: _message(38, kind="video"),
            }
        )
        reader = UserSourceReader(client)
        first, has_more = await reader.list_recent_media(-100555, limit=2)
        self.assertEqual([item.message_id for item in first], [40, 39])
        self.assertTrue(has_more)
        second, has_more = await reader.list_recent_media(-100555, limit=2, offset=2)
        self.assertEqual([item.message_id for item in second], [38])
        self.assertFalse(has_more)

    async def test_list_recent_media_skips_text_messages(self) -> None:
        client = FakeUserClient(
            {
                30: SimpleNamespace(
                    id=30, chat_id=-100555, grouped_id=None, message="广告",
                    photo=None, video=None, document=None, audio=None, voice=None,
                    file=None, media=None,
                ),
                29: _message(29, kind="photo"),
            }
        )
        reader = UserSourceReader(client)
        summaries, _has_more = await reader.list_recent_media(-100555, limit=10)
        self.assertEqual([item.message_id for item in summaries], [29])

    async def test_capture_latest_skips_photo_only_ads(self) -> None:
        newest_photo = _message(50, kind="photo")
        older_video = _message(49, kind="video")
        client = FakeUserClient({50: newest_photo, 49: older_video})
        reader = UserSourceReader(client)
        media = await reader.capture_latest(-100555)
        self.assertEqual([item.source_message_id for item in media], [49])
        self.assertEqual(media[0].kind, MediaKind.VIDEO)

    async def test_capture_latest_photo_only_mode(self) -> None:
        newest_photo = _message(50, kind="photo")
        older_video = _message(49, kind="video")
        client = FakeUserClient({50: newest_photo, 49: older_video})
        reader = UserSourceReader(client)
        media = await reader.capture_latest(-100555, photo_only=True)
        self.assertEqual([item.source_message_id for item in media], [50])
        self.assertEqual(media[0].kind, MediaKind.PHOTO)

    async def test_capture_latest_falls_back_to_photo_without_video(self) -> None:
        client = FakeUserClient({50: _message(50, kind="photo")})
        reader = UserSourceReader(client)
        media = await reader.capture_latest(-100555)
        self.assertEqual([item.source_message_id for item in media], [50])

    async def test_capture_at_expands_the_media_group(self) -> None:
        target = _message(20, grouped_id=99)
        sibling_a = _message(19, grouped_id=99, kind="photo")
        sibling_b = _message(21, grouped_id=99)
        unrelated = _message(120, grouped_id=99)
        client = FakeUserClient({20: target, 19: sibling_a, 21: sibling_b, 120: unrelated})
        reader = UserSourceReader(client)

        media = await reader.capture_at(-100555, 20)
        self.assertEqual([item.source_message_id for item in media], [19, 20, 21])
        self.assertEqual(media[0].kind, MediaKind.PHOTO)
        self.assertEqual(media[1].kind, MediaKind.VIDEO)
        self.assertTrue(all(item.metadata["source_type"] == "user_source" for item in media))

    async def test_capture_at_missing_message_returns_empty(self) -> None:
        reader = UserSourceReader(FakeUserClient())
        self.assertEqual(await reader.capture_at(-100555, 404), [])

    async def test_resolve_link_reads_the_exact_message_and_maps_kinds(self) -> None:
        client = FakeUserClient()
        client.entities["@chan"] = SimpleNamespace(id=5)
        client.messages[42] = _message(42, chat_id=-1005, kind="photo")
        reader = UserSourceReader(client)
        media = await reader.resolve_link("https://t.me/chan/42")
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].kind, MediaKind.PHOTO)
        self.assertEqual(media[0].source_chat_id, -1005)
        self.assertEqual(await reader.resolve_link("https://example.com/x"), [])

    async def test_ordered_chats_follow_whitelist_order(self) -> None:
        reader = UserSourceReader(FakeUserClient())
        reader._allowed_raw = ("@a", "@b")
        reader._allowed_ids = {-1002, -1001}
        reader._chat_labels = {-1001: "@a", -1002: "@b"}
        self.assertEqual(reader.ordered_chats(), [-1001, -1002])
        self.assertEqual(reader.label_for(-1001), "@a")
        self.assertEqual(reader.label_for(-1099), "-1099")

    async def test_missing_message_returns_empty(self) -> None:
        client = FakeUserClient()
        client.entities["@chan"] = SimpleNamespace(id=5)
        reader = UserSourceReader(client)
        self.assertEqual(await reader.resolve_link("https://t.me/chan/999"), [])


class ThumbnailFetchTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, message, payload: bytes = b"jpeg", *, thumb_ok: bool = True):
        calls: list[dict] = []
        streams: list[int] = []

        class _Client:
            async def get_messages(self, chat_id, ids):
                return message

            async def download_media(self, target, file=None, thumb=None):
                calls.append({"message": target, "file": file, "thumb": thumb})
                if not thumb_ok:
                    raise RuntimeError("boom")
                Path(file).write_bytes(payload)
                return str(file)

            def iter_download(self, media, request_size=None):
                streams.append(int(request_size or 0))

                async def generator():
                    for _ in range(4):
                        yield payload

                return generator()

        return _Client(), calls, streams

    async def test_fetch_thumbnail_uses_the_largest_server_thumbnail(self) -> None:
        message = SimpleNamespace(
            photo=SimpleNamespace(sizes=[object()]), document=None, video=None
        )
        client, calls, streams = self._client(message)
        reader = UserSourceReader(client)
        with TemporaryDirectory() as tmp:
            candidate = await reader.fetch_thumbnail(-100, 42, Path(tmp))
            self.assertIsNotNone(candidate)
            self.assertFalse(candidate.needs_frame)
            self.assertTrue(candidate.path.is_file())
            self.assertEqual(calls[0]["thumb"], -1)
            self.assertEqual(streams, [])

    async def test_message_without_thumbnail_is_skipped(self) -> None:
        message = SimpleNamespace(photo=None, document=None, video=None)
        client, calls, _streams = self._client(message)
        reader = UserSourceReader(client)
        with TemporaryDirectory() as tmp:
            self.assertIsNone(await reader.fetch_thumbnail(-100, 42, Path(tmp)))
            self.assertEqual(calls, [])

    async def test_photo_without_thumbnail_falls_back_to_the_original(self) -> None:
        message = SimpleNamespace(
            photo=SimpleNamespace(sizes=[]),
            document=None,
            video=None,
            media=object(),
            number=0,
            file=SimpleNamespace(size=4 * 2048, name="a.jpg", ext=".jpg"),
            id=42,
            chat_id=-100,
            grouped_id=None,
        )
        client, calls, streams = self._client(message, payload=b"p" * 2048)
        reader = UserSourceReader(client)
        with TemporaryDirectory() as tmp:
            candidate = await reader.fetch_thumbnail(-100, 42, Path(tmp))
            self.assertIsNotNone(candidate)
            self.assertFalse(candidate.needs_frame)
            self.assertEqual(candidate.path.suffix, ".jpg")
            self.assertEqual(candidate.path.stat().st_size, 4 * 2048)
            self.assertEqual(calls, [])
            self.assertEqual(streams, [256 * 1024])

    async def test_truncated_photo_download_is_rejected(self) -> None:
        message = SimpleNamespace(
            photo=SimpleNamespace(sizes=[]),
            document=None,
            video=None,
            media=object(),
            file=SimpleNamespace(size=100_000, name="a.jpg", ext=".jpg"),
        )
        client, _calls, _streams = self._client(message, payload=b"p" * 2048)
        reader = UserSourceReader(client)
        with TemporaryDirectory() as tmp:
            self.assertIsNone(await reader.fetch_thumbnail(-100, 42, Path(tmp)))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_oversized_photo_is_never_downloaded(self) -> None:
        message = SimpleNamespace(
            photo=SimpleNamespace(sizes=[]),
            document=None,
            video=None,
            media=object(),
            file=SimpleNamespace(size=5 * 1024 * 1024),
        )
        client, _calls, streams = self._client(message)
        reader = UserSourceReader(client)
        with TemporaryDirectory() as tmp:
            self.assertIsNone(await reader.fetch_thumbnail(-100, 42, Path(tmp)))
            self.assertEqual(streams, [])

    async def test_video_without_thumbnail_requests_a_bounded_fragment(self) -> None:
        message = SimpleNamespace(
            photo=None,
            document=None,
            video=SimpleNamespace(duration=30),
            media=object(),
            file=SimpleNamespace(size=50 * 1024 * 1024),
        )
        client, _calls, streams = self._client(message, payload=b"v" * (3 * 1024 * 1024))
        reader = UserSourceReader(client)
        with TemporaryDirectory() as tmp:
            candidate = await reader.fetch_thumbnail(-100, 42, Path(tmp))
            self.assertIsNotNone(candidate)
            self.assertTrue(candidate.needs_frame)
            self.assertEqual(candidate.path.suffix, ".mp4")
            self.assertEqual(candidate.path.stat().st_size, 4 * 1024 * 1024)
            self.assertEqual(streams, [256 * 1024])

    async def test_oversized_thumbnail_is_rejected_and_removed(self) -> None:
        message = SimpleNamespace(
            photo=SimpleNamespace(sizes=[object()]), document=None, video=None
        )
        client, _calls, _streams = self._client(
            message, payload=b"x" * (1024 * 1024 + 1)
        )
        reader = UserSourceReader(client)
        with TemporaryDirectory() as tmp:
            self.assertIsNone(await reader.fetch_thumbnail(-100, 42, Path(tmp)))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_thumbnail_failure_falls_back_for_photos(self) -> None:
        message = SimpleNamespace(
            photo=SimpleNamespace(sizes=[object()]),
            document=None,
            video=None,
            media=object(),
            file=SimpleNamespace(size=4 * 1024, name="a.jpg", ext=".jpg"),
        )
        client, calls, streams = self._client(message, payload=b"p" * 1024, thumb_ok=False)
        reader = UserSourceReader(client)
        with TemporaryDirectory() as tmp:
            candidate = await reader.fetch_thumbnail(-100, 42, Path(tmp))
            self.assertIsNotNone(candidate)
            self.assertFalse(candidate.needs_frame)
            self.assertEqual(len(calls), 1)
            self.assertEqual(streams, [256 * 1024])


class FakeReader:
    def __init__(self, media: list[IncomingMedia] | None = None) -> None:
        self.media = media or []
        self.captured: list[tuple[int, int]] = []
        self.resolved_links: list[str] = []

    async def capture_at(self, chat_id: int, message_id: int):
        self.captured.append((int(chat_id), int(message_id)))
        return list(self.media)

    async def resolve_link(self, url: str):
        self.resolved_links.append(url)
        return list(self.media)


class SourceMixinHost(IntakeSourceMixin):
    def __init__(self) -> None:
        self._settings = SimpleNamespace(allowed_users=(7,))
        self._source_owner_id = 7
        self._source_reader = None
        self._log = logging.getLogger("test.source")
        self.accepted: list[tuple[int, int, list]] = []
        self.sent: list[tuple[int, str]] = []

    async def _accept_and_schedule(self, chat_id, sender_id, media):
        self.accepted.append((int(chat_id), int(sender_id), list(media)))

    async def _safe_send(self, chat_id, text, **_kwargs):
        self.sent.append((int(chat_id), str(text)))


class FakeGrabCoordinator:
    def __init__(self, count: int = 1, *, label: str = "@src") -> None:
        self.count = count
        self.label = label
        self.calls: list[tuple[int, bool]] = []

    def source_label(self, index: int) -> str:
        return self.label

    async def grab_latest(self, index: int = 0, *, photo_only: bool = False):
        self.calls.append((int(index), bool(photo_only)))
        return (self.count, self.label)


class _GrabEvent:
    chat_id = 7

    def __init__(self, message_id: int = 55) -> None:
        self.message = SimpleNamespace(id=message_id)


class SourceMixinTests(unittest.IsolatedAsyncioTestCase):
    async def test_grab_defaults_to_video_only(self) -> None:
        coordinator = FakeGrabCoordinator()
        host = SourceMixinHost()
        host._source = coordinator
        self.assertTrue(await host.handle_grab(_GrabEvent(), ""))
        self.assertEqual(coordinator.calls, [(0, False)])
        self.assertTrue(any("已抓取 1 个媒体" in text for _chat, text in host.sent))

    async def test_grab_accepts_source_index_and_photo_flag(self) -> None:
        coordinator = FakeGrabCoordinator()
        host = SourceMixinHost()
        host._source = coordinator
        self.assertTrue(await host.handle_grab(_GrabEvent(), "2 photo"))
        self.assertEqual(coordinator.calls, [(1, True)])

    async def test_grab_without_media_points_at_pick(self) -> None:
        host = SourceMixinHost()
        host._source = FakeGrabCoordinator(count=0)
        await host.handle_grab(_GrabEvent(), "")
        self.assertEqual(host.accepted, [])
        self.assertTrue(any("/pick" in text for _chat, text in host.sent))

    async def test_grab_failure_is_reported(self) -> None:
        class _Failing(FakeGrabCoordinator):
            async def grab_latest(self, index: int = 0, *, photo_only: bool = False):
                raise RuntimeError("boom")

        host = SourceMixinHost()
        host._source = _Failing()
        self.assertTrue(await host.handle_grab(_GrabEvent(), ""))
        self.assertTrue(any("抓取失败" in text for _chat, text in host.sent))

    async def test_grab_is_a_noop_without_a_coordinator(self) -> None:
        host = SourceMixinHost()
        self.assertFalse(await host.handle_grab(_GrabEvent(), ""))

    async def test_link_branch_only_handles_telegram_links(self) -> None:
        media = [IncomingMedia(kind=MediaKind.PHOTO, source="telegram:-100:1")]
        host = SourceMixinHost()
        reader = FakeReader(media)
        host._source_reader = reader
        self.assertFalse(await host.handle_source_link("hello", 7, 7))
        self.assertTrue(await host.handle_source_link("https://t.me/foo/1", 7, 7))
        self.assertEqual(host.accepted, [(7, 7, media)])
        self.assertEqual(reader.resolved_links, ["https://t.me/foo/1"])

    async def test_link_without_reader_is_a_noop(self) -> None:
        host = SourceMixinHost()
        self.assertFalse(await host.handle_source_link("https://t.me/foo/1", 7, 7))
        self.assertEqual(host.accepted, [])


class _BoundedDownloader:
    def __init__(self) -> None:
        self.bounded: list[object] = []

    async def download(self, item, target_dir, progress_callback=None):
        raise AssertionError("download should not be used")

    async def download_bounded(self, item, target_dir, *, max_bytes):
        self.bounded.append((item.source, max_bytes))
        return item


class RouterBoundedRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_preview_routes_by_source_type(self) -> None:
        from tgvio.domain.job import MediaItem

        default = _BoundedDownloader()
        routed = _BoundedDownloader()
        router = RoutedMediaDownloader(default, {"user_source": routed})
        item = MediaItem(
            index=0,
            kind=MediaKind.VIDEO,
            source="telegram:-100:5",
            metadata={"source_type": "user_source"},
        )
        await router.download_bounded(item, Path("."), max_bytes=10)
        self.assertEqual(routed.bounded, [(item.source, 10)])
        self.assertEqual(default.bounded, [])

    async def test_bounded_preview_rejects_sources_without_support(self) -> None:
        from tgvio.domain.job import MediaItem

        class _Plain:
            async def download(self, item, target_dir, progress_callback=None):
                return item

        router = RoutedMediaDownloader(_Plain())
        item = MediaItem(index=0, kind=MediaKind.VIDEO, source="u")
        with self.assertRaises(ValueError):
            await router.download_bounded(item, Path("."), max_bytes=10)


class FakeCoordinator:
    def __init__(self, phase: str | None) -> None:
        self.awaiting = phase
        self.calls: list[tuple[str, str]] = []

    def set_awaiting(self, phase):
        self.awaiting = phase

    async def request_code(self, value):
        self.calls.append(("phone", value))
        self.awaiting = "code"
        return "code sent"

    async def submit_code(self, value):
        self.calls.append(("code", value))
        self.awaiting = None
        return "logged in"

    async def submit_password(self, value):
        self.calls.append(("password", value))
        self.awaiting = None
        return "logged in"

    async def add_chat(self, value):
        self.calls.append(("add_chat", value))
        self.awaiting = None
        return "added"


class SourceInputTests(unittest.IsolatedAsyncioTestCase):
    def _host(self, coordinator: FakeCoordinator) -> SourceMixinHost:
        host = SourceMixinHost()
        host._source = coordinator
        return host

    async def test_login_phases_are_dispatched_and_confirmed(self) -> None:
        coordinator = FakeCoordinator("phone")
        host = self._host(coordinator)
        self.assertTrue(await host.handle_source_input("+8613800138000", 7, 7))
        self.assertEqual(coordinator.calls, [("phone", "+8613800138000")])
        self.assertEqual(coordinator.awaiting, "code")
        self.assertTrue(any("code sent" in text for _chat, text in host.sent))

        self.assertTrue(await host.handle_source_input("12345", 7, 7))
        self.assertEqual(coordinator.calls[-1], ("code", "12345"))

    async def test_add_chat_phase(self) -> None:
        coordinator = FakeCoordinator("add_chat")
        host = self._host(coordinator)
        self.assertTrue(await host.handle_source_input("-100123", 7, 7))
        self.assertEqual(coordinator.calls, [("add_chat", "-100123")])

    async def test_cancel_clears_phase(self) -> None:
        coordinator = FakeCoordinator("phone")
        host = self._host(coordinator)
        self.assertTrue(await host.handle_source_input("取消", 7, 7))
        self.assertIsNone(coordinator.awaiting)
        self.assertEqual(coordinator.calls, [])

    async def test_no_phase_or_command_is_ignored(self) -> None:
        host = self._host(FakeCoordinator(None))
        self.assertFalse(await host.handle_source_input("hello", 7, 7))
        command_host = self._host(FakeCoordinator("phone"))
        self.assertFalse(await command_host.handle_source_input("/start", 7, 7))

    async def test_login_error_keeps_the_phase_for_retry(self) -> None:
        from tgvio.adapters.telegram.source_runtime import SourceLoginError

        class _Failing(FakeCoordinator):
            async def submit_code(self, value):
                self.calls.append(("code", value))
                raise SourceLoginError("验证码不正确，请重新输入")

        coordinator = _Failing("code")
        host = self._host(coordinator)
        await host.handle_source_input("0000", 7, 7)
        self.assertEqual(coordinator.awaiting, "code")
        self.assertTrue(any("验证码不正确" in text for _chat, text in host.sent))


if __name__ == "__main__":
    unittest.main()
