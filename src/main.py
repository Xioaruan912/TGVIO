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
    types.BotCommand("mode", "设置 18+ 处理方式"),
    types.BotCommand("webdav", "配置 WebDAV 备份"),
    types.BotCommand("queue", "管理队列"),
    types.BotCommand("begin", "开始合集会话"),
    types.BotCommand("end", "结束合集并发布"),
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
