"""Shared command/menu presentation constants and Telegram keyboards."""

from telethon import Button
from telethon.tl.types import KeyboardButton, KeyboardButtonRow, ReplyKeyboardMarkup


MODE_NAMES = {
    "ask": "每次询问",
    "always_spoiler": "总是雪花遮挡",
    "always_normal": "总是正常",
}

SESSION_BTN_BEGIN = "📥 开始合集"
SESSION_BTN_END = "🛑 结束合集"


def mode_buttons():
    return [
        [Button.inline("🟡 每次询问", "mode:ask")],
        [Button.inline("🔞 总是雪花遮挡", "mode:always_spoiler")],
        [Button.inline("✅ 总是正常", "mode:always_normal")],
    ]


def reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        rows=[
            KeyboardButtonRow(
                buttons=[
                    KeyboardButton(SESSION_BTN_BEGIN),
                    KeyboardButton(SESSION_BTN_END),
                ]
            )
        ],
        resize=True,
        persistent=True,
        single_use=False,
        selective=False,
    )
