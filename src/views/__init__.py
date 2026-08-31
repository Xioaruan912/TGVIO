"""Pure Telegram presentation functions fed by immutable view models."""

from .common import (
    MODE_NAMES,
    SESSION_BTN_BEGIN,
    SESSION_BTN_END,
    mode_buttons,
    reply_keyboard,
)
from .proxy import ProxyViewState, proxy_list_view, proxy_view
from .home import HomeViewState, home_button, home_view
from .job import JobCardView, job_card_view
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
    webdav_probe_view,
    webdav_cfg_view,
)
from .tasks import (
    DurableQueueItemView,
    DurableQueuePageView,
    FailureItemView,
    JobDetailViewState,
    confirmation_view,
    batch_actions_view,
    durable_queue_view,
    failure_center_view,
    job_detail_view,
)
from .stats import stats_view

__all__ = [
    "MODE_NAMES",
    "SESSION_BTN_BEGIN",
    "SESSION_BTN_END",
    "mode_buttons",
    "reply_keyboard",
    "ProxyViewState",
    "proxy_view",
    "proxy_list_view",
    "HomeViewState",
    "home_view",
    "home_button",
    "JobCardView",
    "job_card_view",
    "QueueItemView",
    "PendingQueueItemView",
    "QueueViewState",
    "queue_view",
    "WebDavConfigViewState",
    "webdav_cfg_lines",
    "webdav_cfg_view",
    "webdav_cfg_fields_view",
    "webdav_probe_view",
    "DurableQueueItemView",
    "DurableQueuePageView",
    "FailureItemView",
    "JobDetailViewState",
    "confirmation_view",
    "batch_actions_view",
    "durable_queue_view",
    "failure_center_view",
    "job_detail_view",
    "stats_view",
]
