import asyncio
import logging
import os

from telethon import TelegramClient, functions
from telethon.tl import types

from . import bot, config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_COMMANDS = [
    types.BotCommand("start", "使用说明"),
    types.BotCommand("about", "关于/命令说明"),
    types.BotCommand("status", "查看队列状态"),
    types.BotCommand("progress", "查看下载/上传进度"),
    types.BotCommand("mode", "设置 18+ 处理方式"),
    types.BotCommand("queue", "管理队列"),
    types.BotCommand("cancel", "取消待确认项"),
    types.BotCommand("pause", "暂停队列"),
    types.BotCommand("resume", "恢复队列"),
]


async def _setup_commands(client: TelegramClient) -> None:
    for lang in ("", "zh"):
        await client(
            functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code=lang,
                commands=_COMMANDS,
            )
        )
    await client(
        functions.bots.SetBotMenuButtonRequest(
            user_id="me",
            button=types.BotMenuButtonDefault(),
        )
    )
    logger.info("Bot commands registered")


async def main() -> None:
    os.makedirs("session", exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    client = TelegramClient(
        "session/bot",
        config.API_ID,
        config.API_HASH,
    )
    await client.start(bot_token=config.BOT_TOKEN)

    await _setup_commands(client)
    bot.register_handlers(client)
    logger.info(
        "Bot started. dest=%s allowed=%s",
        config.DEST_CHANNEL,
        sorted(config.ALLOWED_USERS),
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
