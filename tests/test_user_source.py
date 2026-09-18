from __future__ import annotations

import logging
from pathlib import Path
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

    def iter_messages(self, entity, *, min_id=0, max_id=0, limit=None):
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
                if limit is not None and emitted >= int(limit):
                    break
                emitted += 1
                yield message

        return generator()


class UserSourceReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_resolves_whitelist_and_skips_bad_entries(self) -> None:
        client = FakeUserClient()
        client.entities["@good"] = SimpleNamespace(id=111, peer_type="channel")
        reader = UserSourceReader(client, trigger="#tgvio", allowed_chats=["@good", "bad"])
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

    async def test_trigger_filter_requires_whitelist_but_not_a_reply(self) -> None:
        reader = UserSourceReader(FakeUserClient(), trigger="#tgvio", allowed_chats=[])
        reader._allowed_ids = {-100555}
        base = SimpleNamespace(
            outgoing=True,
            raw_text="#tgvio",
            chat_id=-100555,
            message=SimpleNamespace(reply_to_msg_id=None),
        )
        self.assertTrue(reader.is_trigger(base))
        self.assertTrue(
            reader.is_trigger(SimpleNamespace(**{**vars(base), "message": SimpleNamespace(reply_to_msg_id=7)}))
        )
        self.assertFalse(reader.is_trigger(SimpleNamespace(**{**vars(base), "outgoing": False})))
        self.assertFalse(reader.is_trigger(SimpleNamespace(**{**vars(base), "raw_text": "hi"})))
        self.assertFalse(
            reader.is_trigger(
                SimpleNamespace(
                    outgoing=True, raw_text="#tgvio", chat_id=-100999,
                    message=SimpleNamespace(reply_to_msg_id=7),
                )
            )
        )

    async def test_capture_latest_picks_the_newest_media_and_skips_text(self) -> None:
        text_message = SimpleNamespace(
            id=31, chat_id=-100555, grouped_id=None, message="#tgvio",
            photo=None, video=None, document=None, audio=None, voice=None,
            file=None, media=None,
        )
        newest = _message(30, grouped_id=None, kind="document")
        older = _message(29, kind="photo")
        client = FakeUserClient({31: text_message, 30: newest, 29: older})
        reader = UserSourceReader(client, trigger="#tgvio")
        media = await reader.capture_latest(-100555, limit=10)
        self.assertEqual([item.source_message_id for item in media], [30])
        self.assertEqual(media[0].kind, MediaKind.DOCUMENT)

    async def test_capture_reply_expands_the_media_group(self) -> None:
        target = _message(20, grouped_id=99)
        sibling_a = _message(19, grouped_id=99, kind="photo")
        sibling_b = _message(21, grouped_id=99)
        unrelated = _message(120, grouped_id=99)
        client = FakeUserClient({20: target, 19: sibling_a, 21: sibling_b, 120: unrelated})
        reader = UserSourceReader(client, trigger="#tgvio")

        class _Event:
            chat_id = -100555
            message = SimpleNamespace(reply_to_msg_id=20)

        media = await reader.capture_reply(_Event())
        self.assertEqual([item.source_message_id for item in media], [19, 20, 21])
        self.assertEqual(media[0].kind, MediaKind.PHOTO)
        self.assertEqual(media[1].kind, MediaKind.VIDEO)
        self.assertTrue(all(item.metadata["source_type"] == "user_source" for item in media))

    async def test_resolve_link_reads_the_exact_message_and_maps_kinds(self) -> None:
        client = FakeUserClient()
        client.entities["@chan"] = SimpleNamespace(id=5)
        client.messages[42] = _message(42, chat_id=-1005, kind="photo")
        reader = UserSourceReader(client, trigger="#tgvio")
        media = await reader.resolve_link("https://t.me/chan/42")
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].kind, MediaKind.PHOTO)
        self.assertEqual(media[0].source_chat_id, -1005)
        self.assertEqual(await reader.resolve_link("https://example.com/x"), [])

    async def test_seed_cursor_looks_back_a_small_window(self) -> None:
        client = FakeUserClient({40: _message(40), 39: _message(39)})
        reader = UserSourceReader(client, trigger="#tgvio")
        reader._allowed_ids = {-100555}
        cursor = await reader.seed_trigger_cursor()
        self.assertEqual(cursor, {-100555: 35})

    async def test_poll_triggers_finds_only_new_outgoing_matches(self) -> None:
        old = _message(50, kind="document")
        old.message = "#tgvio"
        old.outgoing = True
        target = _message(60, kind="document")
        target.message = "#tgvio"
        target.outgoing = True
        target.reply_to_msg_id = 42
        incoming = _message(61, kind="document")
        incoming.message = "#tgvio"
        incoming.outgoing = False
        other = _message(62, kind="document")
        other.message = "hello"
        other.outgoing = True
        client = FakeUserClient({50: old, 60: target, 61: incoming, 62: other})
        reader = UserSourceReader(client, trigger="#tgvio")
        reader._allowed_ids = {-100555}
        found = await reader.poll_triggers({-100555: 50}, limit=10)
        self.assertEqual(found, [(-100555, 60, 42)])

    async def test_missing_message_returns_empty(self) -> None:
        client = FakeUserClient()
        client.entities["@chan"] = SimpleNamespace(id=5)
        reader = UserSourceReader(client, trigger="#tgvio")
        self.assertEqual(await reader.resolve_link("https://t.me/chan/999"), [])


class FakeSourceClient:
    def __init__(self) -> None:
        self.handlers: list[tuple[object, object]] = []
        self.deleted: list[int] = []

    def add_event_handler(self, handler, event) -> None:
        self.handlers.append((handler, event))

    async def delete_messages(self, chat_id, message_ids, **_kwargs):
        self.deleted.extend(int(mid) for mid in message_ids)
        return True


class FakeReader:
    def __init__(self, media: list[IncomingMedia] | None = None) -> None:
        self.media = media or []
        self.latest_calls: list[int] = []
        self.resolved_links: list[str] = []

    def is_trigger(self, _event) -> bool:
        return True

    async def capture_reply(self, _event):
        return list(self.media)

    async def capture_latest(self, chat_id: int, **_kwargs):
        self.latest_calls.append(int(chat_id))
        return list(self.media)

    async def resolve_link(self, url: str):
        self.resolved_links.append(url)
        return list(self.media)


class SourceMixinHost(IntakeSourceMixin):
    def __init__(self, *, delete_trigger: bool = True) -> None:
        self._settings = SimpleNamespace(source_delete_trigger=delete_trigger)
        self._source_client = FakeSourceClient()
        self._source_owner_id = 7
        self._log = logging.getLogger("test.source")
        self.accepted: list[tuple[int, int, list]] = []
        self.sent: list[tuple[int, str]] = []

    async def _accept_and_schedule(self, chat_id, sender_id, media):
        self.accepted.append((int(chat_id), int(sender_id), list(media)))

    async def _safe_send(self, chat_id, text, **_kwargs):
        self.sent.append((int(chat_id), str(text)))


class _TriggerEvent:
    def __init__(self, message_id: int = 55, *, reply_to: int | None = 7) -> None:
        self.outgoing = True
        self.chat_id = -100555
        self.message = SimpleNamespace(id=message_id, reply_to_msg_id=reply_to)


class SourceMixinTests(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_capture_enqueues_and_deletes_the_trigger(self) -> None:
        media = [IncomingMedia(kind=MediaKind.VIDEO, source="telegram:-100555:10")]
        host = SourceMixinHost()
        host._source_reader = FakeReader(media)
        await host._on_source_trigger(_TriggerEvent(55))
        self.assertEqual(host.accepted, [(7, 7, media)])
        self.assertEqual(host._source_client.deleted, [55])

    async def test_trigger_without_reply_uses_latest_when_enabled(self) -> None:
        media = [IncomingMedia(kind=MediaKind.VIDEO, source="telegram:-100555:9")]
        host = SourceMixinHost()
        reader = FakeReader(media)
        host._source_reader = reader
        host._source = SimpleNamespace(effective_latest=lambda: True)
        await host._on_source_trigger(_TriggerEvent(56, reply_to=None))
        self.assertEqual(reader.latest_calls, [-100555])
        self.assertEqual(host.accepted, [(7, 7, media)])
        self.assertEqual(host._source_client.deleted, [56])

    async def test_trigger_without_reply_warns_when_latest_disabled(self) -> None:
        host = SourceMixinHost()
        reader = FakeReader([IncomingMedia(kind=MediaKind.VIDEO, source="x")])
        host._source_reader = reader
        host._source = SimpleNamespace(effective_latest=lambda: False)
        await host._on_source_trigger(_TriggerEvent(57, reply_to=None))
        self.assertEqual(reader.latest_calls, [])
        self.assertEqual(host.accepted, [])
        self.assertTrue(any("回复" in text for _chat, text in host.sent))

    async def test_trigger_without_media_warns_but_still_cleans_up(self) -> None:
        host = SourceMixinHost()
        host._source_reader = FakeReader([])
        await host._on_source_trigger(_TriggerEvent(58))
        self.assertEqual(host.accepted, [])
        self.assertTrue(any("未能读取" in text for _chat, text in host.sent))
        self.assertEqual(host._source_client.deleted, [58])

    async def test_trigger_delete_can_be_disabled(self) -> None:
        host = SourceMixinHost(delete_trigger=False)
        host._source_reader = FakeReader([])
        await host._on_source_trigger(_TriggerEvent(59))
        self.assertEqual(host._source_client.deleted, [])

    async def test_trigger_misuse_hint_in_the_bot_chat(self) -> None:
        host = SourceMixinHost()
        host._source = SimpleNamespace(effective_trigger=lambda: "#tgvio")
        self.assertFalse(await host.handle_trigger_misuse("其它文字", 7))
        self.assertTrue(await host.handle_trigger_misuse("#tgvio", 7))
        self.assertTrue(any("来源聊天" in text for _chat, text in host.sent))

    async def test_link_branch_only_handles_telegram_links(self) -> None:
        media = [IncomingMedia(kind=MediaKind.PHOTO, source="telegram:-100:1")]
        host = SourceMixinHost()
        reader = FakeReader(media)
        host._source_reader = reader
        self.assertFalse(await host.handle_source_link("hello", 7, 7))
        self.assertTrue(await host.handle_source_link("https://t.me/foo/1", 7, 7))
        self.assertEqual(host.accepted, [(7, 7, media)])
        self.assertEqual(reader.resolved_links, ["https://t.me/foo/1"])

    async def test_no_reader_is_a_noop(self) -> None:
        host = SourceMixinHost()
        host._source_reader = None
        host.register_source_handlers(host._source_client)
        self.assertEqual(host._source_client.handlers, [])
        self.assertFalse(await host.handle_source_link("https://t.me/foo/1", 7, 7))


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
    def __init__(self, phase: str | None, *, delete: bool = True) -> None:
        self.awaiting = phase
        self.calls: list[tuple[str, str]] = []
        self._delete = delete

    def set_awaiting(self, phase):
        self.awaiting = phase

    def effective_delete_trigger(self) -> bool:
        return self._delete

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
