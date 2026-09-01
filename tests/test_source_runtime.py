import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from telethon.tl.types import (
    InputPeerChannel,
    InputPeerUser,
    MessageMediaDocument,
    MessageMediaPhoto,
)
from telethon.utils import get_peer_id

from src.services.source_runtime import (
    SourceAccessResult,
    SourceProfileRuntime,
    probe_source_access,
)
from tests.fakes import FakeClock


class _ControlledSleep:
    def __init__(self) -> None:
        self.calls: list[tuple[float, asyncio.Event]] = []

    async def __call__(self, delay: float) -> None:
        event = asyncio.Event()
        self.calls.append((float(delay), event))
        await event.wait()

    async def wait_for_calls(self, count: int) -> None:
        for _ in range(20):
            if len(self.calls) >= count:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"expected {count} sleep calls, got {len(self.calls)}")

    def release(self, index: int) -> None:
        self.calls[index][1].set()


class _Sources:
    def __init__(self, profiles=(), events=()) -> None:
        self.profiles = list(profiles)
        self.events = list(events)
        self.failed: list[tuple[tuple[int, ...], str]] = []

    async def list_profiles(self):
        return list(self.profiles)

    async def received_events(
        self,
        *,
        limit: int = 1000,
        after_created_at=None,
        after_id: int = 0,
    ):
        values = sorted(self.events, key=lambda event: (event.created_at, event.id))
        if after_created_at is not None:
            values = [
                event
                for event in values
                if (event.created_at, event.id) > (after_created_at, after_id)
            ]
        return values[:limit]

    async def mark_failed(self, event_ids, code: str) -> None:
        self.failed.append((tuple(event_ids), code))


class _RecoveryClient:
    def __init__(self, messages: dict[str, list[object]]) -> None:
        self.messages = messages
        self.get_messages_calls: list[tuple[str, tuple[int, ...]]] = []
        self.sent_messages: list[tuple[int, str]] = []

    async def get_messages(self, peer: str, *, ids: list[int]):
        self.get_messages_calls.append((peer, tuple(ids)))
        allowed = set(ids)
        return [item for item in self.messages.get(peer, []) if item.id in allowed]

    async def send_message(self, peer: int, text: str, **_kwargs):
        self.sent_messages.append((peer, text))


def _profile(profile_id: int, *, peer: str | None = None, sequential: float = 120.0):
    return SimpleNamespace(
        id=profile_id,
        name=f"来源 {profile_id}",
        source_peer=peer or f"@source{profile_id}",
        source_peer_id=-1000000000000 - profile_id,
        destination_profile_id=1,
        owner_user_id=42,
        enabled=True,
        album_gather_seconds=2.0,
        sequential_video_gather_seconds=sequential,
        spoiler_policy="normal",
        caption_policy="preserve",
        backup_policy="inherit",
    )


def _event(event_id: int, profile_id: int, message_id: int, created_at: float, grouped_id=None):
    return SimpleNamespace(
        id=event_id,
        source_profile_id=profile_id,
        source_message_id=message_id,
        grouped_id=grouped_id,
        created_at=created_at,
    )


def _video(message_id: int, grouped_id=None):
    return SimpleNamespace(
        id=message_id,
        grouped_id=grouped_id,
        media=MessageMediaDocument(
            document=SimpleNamespace(mime_type="video/mp4", attributes=[])
        ),
    )


def _photo(message_id: int, grouped_id=None):
    return SimpleNamespace(
        id=message_id,
        grouped_id=grouped_id,
        media=MessageMediaPhoto(photo=None),
    )


class SourceAccessProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_checks_the_bot_itself_not_default_channel_permissions(self) -> None:
        entity = InputPeerChannel(channel_id=123, access_hash=456)
        me = InputPeerUser(user_id=7, access_hash=8)
        client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value=entity),
            get_me=AsyncMock(return_value=me),
            get_permissions=AsyncMock(return_value=SimpleNamespace(is_admin=True)),
        )

        result = await probe_source_access(
            client,
            "@source",
            expected_peer_id=get_peer_id(entity),
        )

        self.assertTrue(result.ok)
        client.get_me.assert_awaited_once_with(input_peer=True)
        client.get_permissions.assert_awaited_once_with(entity, me)

    async def test_non_member_is_a_safe_failed_verification(self) -> None:
        error_type = type("UserNotParticipantError", (Exception,), {})
        entity = InputPeerChannel(channel_id=123, access_hash=456)
        me = InputPeerUser(user_id=7, access_hash=8)
        client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value=entity),
            get_me=AsyncMock(return_value=me),
            get_permissions=AsyncMock(side_effect=error_type("private details")),
        )

        result = await probe_source_access(client, "@source")

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "bot_not_member")
        self.assertNotIn("private details", result.message)
        client.get_permissions.assert_awaited_once_with(entity, me)


class SourceProfileRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, *, clock=None, sleep=None, sources=None, client=None, access_probe=None):
        self.enqueued: list[tuple[int, list[int], list[int]]] = []

        async def enqueue(profile, messages, event_ids):
            self.enqueued.append(
                (profile.id, [message.id for message in messages], list(event_ids))
            )

        kwargs = {}
        if access_probe is not None:
            kwargs["access_probe"] = access_probe
        runtime = SourceProfileRuntime(
            client=client or SimpleNamespace(),
            sources=sources or _Sources(),
            enqueue=enqueue,
            clock=(clock.time if clock is not None else None) or __import__("time").time,
            sleep=sleep or asyncio.sleep,
            **kwargs,
        )
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def test_sequential_video_timer_resets_after_each_video(self) -> None:
        clock = FakeClock(100.0)
        sleep = _ControlledSleep()
        runtime = self._runtime(clock=clock, sleep=sleep)
        profile = _profile(1)

        await runtime.accept(profile, _video(1), 101, grouped_id=None, received_at=100.0)
        await sleep.wait_for_calls(1)
        clock.advance(30.0)
        await runtime.accept(profile, _video(2), 102, grouped_id=None, received_at=130.0)
        await sleep.wait_for_calls(2)

        sleep.release(0)
        await asyncio.sleep(0)
        self.assertEqual(self.enqueued, [])
        sleep.release(1)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(self.enqueued, [(1, [1, 2], [101, 102])])

    async def test_tenth_sequential_video_flushes_immediately(self) -> None:
        clock = FakeClock(100.0)
        runtime = self._runtime(clock=clock, sleep=_ControlledSleep())
        profile = _profile(1)

        for message_id in range(1, 11):
            await runtime.accept(
                profile,
                _video(message_id),
                100 + message_id,
                grouped_id=None,
                received_at=100.0 + message_id,
            )

        self.assertEqual(
            self.enqueued,
            [(1, list(range(1, 11)), list(range(101, 111)))],
        )
        self.assertEqual(runtime.pending_bundle_count, 0)

    async def test_photo_is_a_boundary_and_keeps_enqueue_order(self) -> None:
        clock = FakeClock(100.0)
        runtime = self._runtime(clock=clock, sleep=_ControlledSleep())
        profile = _profile(1)

        await runtime.accept(profile, _video(1), 101, grouped_id=None, received_at=100.0)
        await runtime.accept(profile, _video(2), 102, grouped_id=None, received_at=101.0)
        await runtime.accept(profile, _photo(3), 103, grouped_id=None, received_at=102.0)

        self.assertEqual(
            self.enqueued,
            [(1, [1, 2], [101, 102]), (1, [3], [103])],
        )

    async def test_profiles_are_isolated(self) -> None:
        runtime = self._runtime(sleep=_ControlledSleep())
        first = _profile(1)
        second = _profile(2)

        await runtime.accept(first, _video(1), 101, grouped_id=None)
        await runtime.accept(second, _video(2), 102, grouped_id=None)
        await runtime.boundary(first)

        self.assertEqual(self.enqueued, [(1, [1], [101])])
        self.assertEqual(runtime.pending_bundle_count, 1)

    async def test_native_group_uses_short_fixed_window(self) -> None:
        clock = FakeClock(100.0)
        sleep = _ControlledSleep()
        runtime = self._runtime(clock=clock, sleep=sleep)
        profile = _profile(1)

        await runtime.accept(profile, _photo(1, 55), 101, grouped_id=55, received_at=100.0)
        await sleep.wait_for_calls(1)
        clock.advance(1.0)
        await runtime.accept(profile, _photo(2, 55), 102, grouped_id=55, received_at=101.0)

        self.assertEqual(len(sleep.calls), 1)
        self.assertAlmostEqual(sleep.calls[0][0], 2.0)
        sleep.release(0)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(self.enqueued, [(1, [1, 2], [101, 102])])

    async def test_close_cancels_buffers_without_marking_events_failed(self) -> None:
        sources = _Sources()
        runtime = self._runtime(sources=sources, sleep=_ControlledSleep())
        await runtime.accept(_profile(1), _video(1), 101, grouped_id=None)

        await runtime.close()

        self.assertEqual(self.enqueued, [])
        self.assertEqual(sources.failed, [])
        self.assertEqual(runtime.pending_bundle_count, 0)

    async def test_startup_recovers_only_exact_ids_and_keeps_remaining_window(self) -> None:
        due = _profile(1, peer="@due")
        future = _profile(2, peer="@future")
        missing = _profile(3, peer="@missing")
        events = [
            _event(11, 1, 101, 800.0),
            _event(12, 1, 102, 850.0),
            _event(21, 2, 201, 950.0),
            _event(31, 3, 301, 950.0),
        ]
        sources = _Sources([due, future, missing], events)
        client = _RecoveryClient(
            {
                "@due": [_video(101), _video(102)],
                "@future": [_video(201)],
                "@missing": [],
            }
        )
        clock = FakeClock(1000.0)
        sleep = _ControlledSleep()

        async def access_probe(_client, _peer, *, expected_peer_id=None):
            return SourceAccessResult(True, "ok", "ok", expected_peer_id)

        runtime = self._runtime(
            clock=clock,
            sleep=sleep,
            sources=sources,
            client=client,
            access_probe=access_probe,
        )

        report = await runtime.startup()

        self.assertEqual(report.restored_events, 3)
        self.assertEqual(report.failed_events, 1)
        self.assertEqual(
            client.get_messages_calls,
            [
                ("@due", (101, 102)),
                ("@future", (201,)),
                ("@missing", (301,)),
            ],
        )
        self.assertEqual(self.enqueued, [(1, [101, 102], [11, 12])])
        self.assertIn(((31,), "source_message_unavailable"), sources.failed)
        self.assertEqual(runtime.pending_bundle_count, 1)
        await sleep.wait_for_calls(1)
        self.assertAlmostEqual(sleep.calls[-1][0], 70.0)

    async def test_inaccessible_startup_defers_received_event_and_warns_owner(self) -> None:
        profile = _profile(1, peer="@private")
        sources = _Sources([profile], [_event(11, 1, 101, 900.0)])
        client = _RecoveryClient({"@private": [_video(101)]})

        async def access_probe(_client, _peer, *, expected_peer_id=None):
            return SourceAccessResult(False, "bot_not_member", "Bot 不是该来源的有效成员")

        runtime = self._runtime(
            sources=sources,
            client=client,
            access_probe=access_probe,
            sleep=_ControlledSleep(),
        )

        report = await runtime.startup()

        self.assertEqual(report.deferred_events, 1)
        self.assertEqual(client.get_messages_calls, [])
        self.assertEqual(sources.failed, [])
        self.assertEqual(len(client.sent_messages), 1)
        self.assertNotIn(profile.source_peer, client.sent_messages[0][1])


if __name__ == "__main__":
    unittest.main()
