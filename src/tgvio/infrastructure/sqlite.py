from __future__ import annotations

from tgvio.infrastructure.sqlite_archive import SQLiteArchiveRepositoryMixin
from tgvio.infrastructure.sqlite_archive_deletion import SQLiteArchiveDeletionRepositoryMixin
from tgvio.infrastructure.sqlite_base import SQLiteRepositoryBase
from tgvio.infrastructure.sqlite_collection_editing import (
    SQLiteCollectionEditingRepositoryMixin,
)
from tgvio.infrastructure.sqlite_control import SQLiteControlRepositoryMixin
from tgvio.infrastructure.sqlite_favorites import SQLiteFavoritesRepositoryMixin
from tgvio.infrastructure.sqlite_intake import SQLiteIntakeRepositoryMixin
from tgvio.infrastructure.sqlite_jobs import SQLiteJobRepositoryMixin
from tgvio.infrastructure.sqlite_history import SQLiteHistoryRepositoryMixin
from tgvio.infrastructure.sqlite_maintenance import SQLiteMaintenanceRepositoryMixin
from tgvio.infrastructure.sqlite_notifications import SQLiteNotificationRepositoryMixin
from tgvio.infrastructure.sqlite_observability import SQLiteObservabilityRepositoryMixin
from tgvio.infrastructure.sqlite_operations import SQLiteOperationRepositoryMixin
from tgvio.infrastructure.sqlite_publish import SQLitePublishRepositoryMixin
from tgvio.infrastructure.sqlite_previews import SQLitePreviewRepositoryMixin
from tgvio.infrastructure.sqlite_scheduler import SQLiteSchedulerRepositoryMixin
from tgvio.infrastructure.sqlite_suggestions import SQLiteSuggestionRepositoryMixin


class SQLiteJobRepository(
    SQLiteJobRepositoryMixin,
    SQLiteHistoryRepositoryMixin,
    SQLiteIntakeRepositoryMixin,
    SQLiteCollectionEditingRepositoryMixin,
    SQLiteFavoritesRepositoryMixin,
    SQLitePublishRepositoryMixin,
    SQLitePreviewRepositoryMixin,
    SQLiteOperationRepositoryMixin,
    SQLiteArchiveRepositoryMixin,
    SQLiteArchiveDeletionRepositoryMixin,
    SQLiteControlRepositoryMixin,
    SQLiteObservabilityRepositoryMixin,
    SQLiteNotificationRepositoryMixin,
    SQLiteMaintenanceRepositoryMixin,
    SQLiteSchedulerRepositoryMixin,
    SQLiteSuggestionRepositoryMixin,
    SQLiteRepositoryBase,
):
    """Compatibility facade over the split SQLite repository modules."""

    pass
