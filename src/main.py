import asyncio
import logging
import os
import signal

from telethon import TelegramClient, functions
from telethon.tl import types

from . import bot, config
from .repository import SQLiteRepository
from .security import (
    install_redacting_logging,
    secure_private_directory,
    secure_private_file,
)
from .services import RuntimeHeartbeat
from .storage import JsonStore

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
)
install_redacting_logging(fmt=_LOG_FORMAT)
logger = logging.getLogger(__name__)

_COMMANDS = [
    types.BotCommand("start", "使用说明"),
    types.BotCommand("about", "关于/命令说明"),
    types.BotCommand("stats", "运行状态与健康信息"),
    types.BotCommand("health", "本地健康检查"),
    types.BotCommand("diag", "导出脱敏诊断"),
    types.BotCommand("mode", "设置 18+ 处理方式"),
    types.BotCommand("profiles", "管理发布目的地"),
    types.BotCommand("sources", "管理自动来源"),
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
        asyncio.ensure_future(client.disconnect())

    try:
        loop.add_signal_handler(signal.SIGTERM, request_shutdown)
    except (NotImplementedError, RuntimeError):
        logger.warning("SIGTERM handler unavailable on this event loop")


async def main() -> None:
    settings = config.Settings.from_env(strict=True)
    logger.info("Static settings loaded: %s", settings.safe_summary())
    secure_private_file(".env")
    if not secure_private_directory("session"):
        raise RuntimeError("session runtime directory is unavailable or unsafe")
    if not secure_private_directory(settings.download_dir):
        raise RuntimeError("download runtime directory is unavailable or unsafe")

    repository = SQLiteRepository(
        "session/state.sqlite3",
        download_root=settings.download_dir,
    )
    pipeline = None
    heartbeat = RuntimeHeartbeat("session/runtime-health.json")
    await repository.open()
    heartbeat.start()
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
        legacy_webdav = JsonStore(os.path.join("session", "webdav.json"), {}).load()
        legacy_backup_policy = (
            str(legacy_webdav.get("backup_policy") or "best_effort")
            if isinstance(legacy_webdav, dict)
            else "best_effort"
        )
        if legacy_backup_policy not in {"best_effort", "required"}:
            legacy_backup_policy = "best_effort"
        env_destination_profile = await repository.ensure_env_destination_profile(
            destination_peer=settings.dest_channel,
            channel_at=settings.channel_at,
            group_at=settings.group_at,
            cover_mode=settings.cover_mode,
            forward_caption=settings.forward_caption,
            default_spoiler_mode="always_normal",
            backup_policy=legacy_backup_policy,
        )
        default_destination_profile = (
            await repository.get_default_destination_profile()
            or env_destination_profile
        )
        interrupted_sources = await repository.interrupt_received_source_events()
        if interrupted_sources:
            logger.warning(
                "Marked %d in-flight source event(s) interrupted after restart; no history replay",
                interrupted_sources,
            )
        reconciled = await repository.reconcile_daily_stats()
        if reconciled:
            logger.info("Daily stats reconciled: %d metrics", reconciled)

        client = TelegramClient(
            "session/bot",
            settings.api_id,
            settings.api_hash,
            request_retries=8,
            connection_retries=8,
        )
        await client.start(bot_token=settings.bot_token)
        _install_sigterm_handler(client)

        await _setup_commands(client)
        pipeline = bot.register_handlers(
            client,
            repository=repository,
            default_destination_profile=default_destination_profile,
            start_workers=False,
            settings=settings,
        )
        await pipeline.recover_from_repository()
        pipeline.start()
        await pipeline.apply_proxy_on_start()
        heartbeat.set_ready(True)
        logger.info("Bot started. settings=%s", settings.safe_summary())
        await client.run_until_disconnected()
    finally:
        heartbeat.set_ready(False)
        if pipeline is not None:
            await pipeline.shutdown()
        await repository.close()
        await heartbeat.stop()


if __name__ == "__main__":
    asyncio.run(main())
