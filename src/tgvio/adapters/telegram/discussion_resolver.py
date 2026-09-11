from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import time
import urllib.parse
import urllib.request


class DiscussionResolveError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DiscussionRoot:
    chat_id: int
    message_id: int
    source_message_id: int


class BotApiDiscussionResolver:
    """Resolve a channel post's linked discussion root using bot-safe Bot API facts.

    MTProto messages.getDiscussionMessage is user-only. Telegram automatically
    forwards channel posts into the linked discussion group and pins that
    forwarded message; Bot API getChat exposes both linked_chat_id and the
    current pinned automatic-forward message.
    """

    def __init__(
        self,
        bot_token: str,
        *,
        timeout_seconds: float = 8.0,
        poll_interval_seconds: float = 0.25,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        if not bot_token:
            raise ValueError("Bot API discussion resolver requires bot token")
        self._bot_token = bot_token
        self._timeout_seconds = max(0.0, float(timeout_seconds))
        self._poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self._request_timeout_seconds = max(1.0, float(request_timeout_seconds))

    async def resolve(
        self,
        channel_chat_id: str | int,
        channel_message_ids: tuple[int, ...],
    ) -> DiscussionRoot | None:
        candidates = tuple(dict.fromkeys(int(value) for value in channel_message_ids))
        if not candidates:
            return None
        return await asyncio.to_thread(
            self._resolve_sync,
            str(channel_chat_id),
            candidates,
        )

    def _resolve_sync(
        self,
        channel_chat_id: str,
        candidates: tuple[int, ...],
    ) -> DiscussionRoot | None:
        channel = self._api_sync("getChat", {"chat_id": channel_chat_id})
        linked_chat_id = channel.get("linked_chat_id")
        if linked_chat_id is None:
            return None

        deadline = time.monotonic() + self._timeout_seconds
        while True:
            group = self._api_sync("getChat", {"chat_id": str(linked_chat_id)})
            pinned = group.get("pinned_message") or {}
            if isinstance(pinned, dict) and pinned.get("is_automatic_forward"):
                source_message_id = self._source_message_id(pinned)
                root_message_id = pinned.get("message_id")
                if (
                    source_message_id is not None
                    and int(source_message_id) in candidates
                    and root_message_id is not None
                ):
                    return DiscussionRoot(
                        chat_id=int(linked_chat_id),
                        message_id=int(root_message_id),
                        source_message_id=int(source_message_id),
                    )
            if time.monotonic() >= deadline:
                return None
            time.sleep(self._poll_interval_seconds)

    def _api_sync(self, method: str, params: dict[str, str]) -> dict[str, object]:
        query = urllib.parse.urlencode(params)
        url = f"https://api.telegram.org/bot{self._bot_token}/{method}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=self._request_timeout_seconds) as response:
                payload = json.load(response)
        except Exception as exc:
            # Never surface urllib's URL because it contains the bot token.
            raise DiscussionResolveError(
                f"Telegram Bot API {method} request failed: {type(exc).__name__}"
            ) from None
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise DiscussionResolveError(f"Telegram Bot API {method} returned failure")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise DiscussionResolveError(f"Telegram Bot API {method} returned invalid result")
        return result

    @staticmethod
    def _source_message_id(message: dict[str, object]) -> int | None:
        origin = message.get("forward_origin")
        if isinstance(origin, dict) and origin.get("message_id") is not None:
            return int(origin["message_id"])
        legacy = message.get("forward_from_message_id")
        return int(legacy) if legacy is not None else None

