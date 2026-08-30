import asyncio
import logging
import os
import signal

from telethon import TelegramClient, functions
from telethon.tl import types

from . import bot, config
from .repository import SQLiteRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_COMMANDS = [
    types.BotCommand("start", "使用说明"),
    types.BotCommand("about", "关于/命令说明"),
    types.BotCommand("mode", "设置 18+ 处理方式"),
    types.BotCommand("webdav", "配置 WebDAV 备份链接"),
    types.BotCommand("webdavlogs", "查看上传记录 / 本地缓存"),
    types.BotCommand("proxy", "代理设置（HTTP）"),
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


def _install_sigterm_handler(client: TelegramClient) -> None:
    """Make container SIGTERM disconnect Telethon so main can run shutdown."""
    loop = asyncio.get_running_loop()
    fired = False

    def request_shutdown() -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        logger.info("SIGTERM received; disconnecting Telegram client")
        loop.create_task(client.disconnect())

    try:
        loop.add_signal_handler(signal.SIGTERM, request_shutdown)
    except (NotImplementedError, RuntimeError):
        logger.warning("SIGTERM handler unavailable on this event loop")


async def main() -> None:
    os.makedirs("session", exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    repository = SQLiteRepository(
        "session/state.sqlite3",
        download_root=config.DOWNLOAD_DIR,
    )
    pipeline = None
    await repository.open()
    try:
        applied = await repository.migrate()
        check = await repository.self_check()
        logger.info(
            "SQLite repository ready. schema=%s integrity=%s",
            await repository.schema_versions(),
            check["integrity"],
        )
        if applied:
            logger.info("SQLite migrations applied on startup: %s", applied)

        client = TelegramClient(
            "session/bot",
            config.API_ID,
            config.API_HASH,
            request_retries=8,
            connection_retries=8,
        )
        await client.start(bot_token=config.BOT_TOKEN)
        _install_sigterm_handler(client)

        await _setup_commands(client)
        pipeline = bot.register_handlers(client, repository=repository, start_workers=False)
        await pipeline.recover_from_repository()
        pipeline.start()
        await pipeline.apply_proxy_on_start()
        logger.info(
            "Bot started. dest=%s allowed=%s",
            config.DEST_CHANNEL,
            sorted(config.ALLOWED_USERS),
        )
        await client.run_until_disconnected()
    finally:
        if pipeline is not None:
            await pipeline.shutdown()
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
