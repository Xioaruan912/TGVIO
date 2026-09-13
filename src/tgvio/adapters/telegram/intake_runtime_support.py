from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Iterable

from telethon import Button, TelegramClient, events

from tgvio.adapters.telegram.user_messages import (
    describe_archive_failure,
    describe_job_failure,
)
from tgvio.application.intake import (
    CollectionEmptyError,
    IncomingMedia,
    IntakeAcceptResult,
    IntakeService,
)
from tgvio.application.auto_recovery import (
    archive_failure_waits_for_recovery,
    archive_recovery_state,
    job_failure_waits_for_recovery,
    job_recovery_state,
)
from tgvio.application.job_control import JobCancelRequested, JobHoldRequested
from tgvio.application.job_runner import JobRunner
from tgvio.application.scheduler import (
    OrderedPublishDispatcher,
    PhaseClaimGuard,
    PhaseClaimLostError,
)
from tgvio.config import Settings
from tgvio.domain.archive import ArchivePackage, ArchivePackageState
from tgvio.domain.intake import JobDisplayMessage, SpoilerMode
from tgvio.domain.job import Job, JobState, MediaKind
from tgvio.domain.progress import JobProgress
from tgvio.infrastructure.url_security import validate_url_syntax
from tgvio.observability import log_event


COLLECTION_BEGIN_BUTTON = "📥 开始合集"
COLLECTION_END_BUTTON = "🛑 结束并发布"


@dataclass(slots=True)
class _PendingBatch:
    chat_id: int
    sender_id: int
    media: list[IncomingMedia] = field(default_factory=list)
    first_seen: float = 0.0
    flush_task: asyncio.Task | None = None
