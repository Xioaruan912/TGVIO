"""Pure Telegram presentation functions fed by immutable view models."""

from .common import (
    MODE_NAMES,
    SESSION_BTN_BEGIN,
    SESSION_BTN_END,
    mode_buttons,
    reply_keyboard,
)
from .proxy import ProxyViewState, proxy_list_view, proxy_view
from .queue import (
    PendingQueueItemView,
    QueueItemView,
    QueueViewState,
    queue_view,
)
from .webdav import (
    WebDavConfigViewState,
    webdav_cfg_fields_view,
    webdav_cfg_lines,
    webdav_cfg_view,
)

__all__ = [
    "MODE_NAMES",
    "SESSION_BTN_BEGIN",
    "SESSION_BTN_END",
    "mode_buttons",
    "reply_keyboard",
    "ProxyViewState",
    "proxy_view",
    "proxy_list_view",
    "QueueItemView",
    "PendingQueueItemView",
    "QueueViewState",
    "queue_view",
    "WebDavConfigViewState",
    "webdav_cfg_lines",
    "webdav_cfg_view",
    "webdav_cfg_fields_view",
]
