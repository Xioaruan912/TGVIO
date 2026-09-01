"""Telegram handler registration package."""

from .callbacks import CallbackRouter, build_callback_router, register_callback_handler
from .collection import register_collection_commands
from .common import HandlerContext
from .jobs import register_job_commands
from .private import register_private_handler
from .proxy import register_proxy_command
from .settings import register_setting_commands


def install_handlers(ctx: HandlerContext) -> None:
    register_setting_commands(ctx)
    register_proxy_command(ctx)
    register_collection_commands(ctx)
    register_job_commands(ctx)
    register_callback_handler(ctx)
    register_private_handler(ctx)


__all__ = [
    "CallbackRouter",
    "HandlerContext",
    "build_callback_router",
    "install_handlers",
]
