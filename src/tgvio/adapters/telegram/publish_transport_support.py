from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable, Protocol

from telethon import Button, TelegramClient, helpers, utils
from telethon.errors.rpcerrorlist import MediaEmptyError, MediaInvalidError
from telethon.tl import functions, types

from tgvio.application.ports import (
    PublishTransportPartialError,
    PublishTransportUncertainError,
)
from tgvio.domain.job import Job, MediaItem, MediaKind
from tgvio.domain.publish import (
    PublishEffect,
    PublishReceipt,
    PublishStep,
    PublishStepKind,
    PublishTarget,
)
from tgvio.infrastructure.media_transformer import FFmpegMediaTransformer
from tgvio.adapters.telegram.discussion_resolver import DiscussionRoot
from tgvio.adapters.telegram.uploads import BoundedTelegramUploader
from tgvio.observability import log_event


class UnsupportedPublishStep(RuntimeError):
    pass


class DiscussionResolver(Protocol):
    async def resolve(
        self,
        channel_chat_id: str | int,
        channel_message_ids: tuple[int, ...],
    ) -> DiscussionRoot | None: ...
