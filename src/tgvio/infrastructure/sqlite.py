from __future__ import annotations

from tgvio.infrastructure.sqlite_archive import SQLiteArchiveRepositoryMixin
from tgvio.infrastructure.sqlite_base import SQLiteRepositoryBase
from tgvio.infrastructure.sqlite_control import SQLiteControlRepositoryMixin
from tgvio.infrastructure.sqlite_intake import SQLiteIntakeRepositoryMixin
from tgvio.infrastructure.sqlite_jobs import SQLiteJobRepositoryMixin
from tgvio.infrastructure.sqlite_notifications import SQLiteNotificationRepositoryMixin
from tgvio.infrastructure.sqlite_observability import SQLiteObservabilityRepositoryMixin
from tgvio.infrastructure.sqlite_operations import SQLiteOperationRepositoryMixin
from tgvio.infrastructure.sqlite_publish import SQLitePublishRepositoryMixin
from tgvio.infrastructure.sqlite_scheduler import SQLiteSchedulerRepositoryMixin


class SQLiteJobRepository(
    SQLiteJobRepositoryMixin,
    SQLiteIntakeRepositoryMixin,
    SQLitePublishRepositoryMixin,
    SQLiteOperationRepositoryMixin,
    SQLiteArchiveRepositoryMixin,
    SQLiteControlRepositoryMixin,
    SQLiteObservabilityRepositoryMixin,
    SQLiteNotificationRepositoryMixin,
    SQLiteSchedulerRepositoryMixin,
    SQLiteRepositoryBase,
):
    """Compatibility facade over the split SQLite repository modules."""

    pass
