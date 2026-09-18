"""Pure parsing of Telegram message links.

Only message links are accepted. Invite links, usernames without a message id
and non-Telegram URLs return ``None`` so the caller can fall back to the normal
URL pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

_TME_HOSTS = {
    "t.me",
    "www.t.me",
    "telegram.me",
    "www.telegram.me",
    "telegram.dog",
    "www.telegram.dog",
}


@dataclass(frozen=True, slots=True)
class TelegramLink:
    """A resolved Telegram message reference: ``@username`` or ``-100...`` id."""

    chat: str | int
    message_id: int


def _chat_ref(value: str) -> str | int:
    raw = value.strip().lstrip("@")
    if raw.isdigit():
        return int(raw)
    return f"@{raw}"


def parse_telegram_link(value: str | None) -> TelegramLink | None:
    text = (value or "").strip()
    if not text:
        return None

    if text.startswith("tg://"):
        parts = urlsplit(text)
        query = parse_qs(parts.query)
        domain = (query.get("domain") or [""])[0].strip()
        post = (query.get("post") or [""])[0].strip()
        if domain and post.isdigit():
            return TelegramLink(chat=_chat_ref(domain), message_id=int(post))
        return None

    parts = urlsplit(text if "://" in text else f"https://{text}")
    if (parts.hostname or "").lower() not in _TME_HOSTS:
        return None

    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2:
        return None
    head = segments[0]
    if head.lower() == "c":
        # /c/<internal_channel_id>/<message_id>
        if len(segments) < 3 or not segments[1].isdigit() or not segments[2].isdigit():
            return None
        return TelegramLink(chat=int(f"-100{segments[1]}"), message_id=int(segments[2]))
    if head.startswith("+") or head.lower() == "joinchat":
        return None
    if not segments[1].isdigit():
        return None
    return TelegramLink(chat=_chat_ref(head), message_id=int(segments[1]))


def is_telegram_link(value: str | None) -> bool:
    return parse_telegram_link(value) is not None
