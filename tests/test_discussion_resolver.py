from __future__ import annotations

import unittest

from tgvio.adapters.telegram.discussion_resolver import BotApiDiscussionResolver


class FixtureResolver(BotApiDiscussionResolver):
    def __init__(self, *, source_message_id: int) -> None:
        super().__init__("fixture-token", timeout_seconds=0)
        self.source_message_id = source_message_id
        self.calls: list[tuple[str, dict[str, str]]] = []

    def _api_sync(self, method: str, params: dict[str, str]):
        self.calls.append((method, params))
        if len(self.calls) == 1:
            return {"linked_chat_id": -100222}
        return {
            "pinned_message": {
                "message_id": 700,
                "is_automatic_forward": True,
                "forward_origin": {
                    "type": "channel",
                    "message_id": self.source_message_id,
                },
            }
        }


class BotApiDiscussionResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_matching_pinned_automatic_forward(self) -> None:
        resolver = FixtureResolver(source_message_id=501)
        root = await resolver.resolve("-1000000000111", (500, 501))
        assert root is not None
        self.assertEqual(root.chat_id, -100222)
        self.assertEqual(root.message_id, 700)
        self.assertEqual(root.source_message_id, 501)

    async def test_rejects_pinned_message_from_another_channel_post(self) -> None:
        resolver = FixtureResolver(source_message_id=999)
        root = await resolver.resolve("-1000000000111", (500, 501))
        self.assertIsNone(root)

