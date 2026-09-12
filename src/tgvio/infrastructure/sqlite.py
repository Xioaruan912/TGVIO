from __future__ import annotations

from tgvio.infrastructure.sqlite_archive import SQLiteArchiveRepositoryMixin
from tgvio.infrastructure.sqlite_base import SQLiteRepositoryBase
from tgvio.infrastructure.sqlite_control import SQLiteControlRepositoryMixin
from tgvio.infrastructure.sqlite_jobs import SQLiteJobRepositoryMixin
from tgvio.infrastructure.sqlite_observability import SQLiteObservabilityRepositoryMixin
from tgvio.infrastructure.sqlite_publish import SQLitePublishRepositoryMixin


class SQLiteJobRepository(
    SQLiteJobRepositoryMixin,
    SQLitePublishRepositoryMixin,
    SQLiteArchiveRepositoryMixin,
    SQLiteControlRepositoryMixin,
    SQLiteObservabilityRepositoryMixin,
    SQLiteRepositoryBase,
):
    """Compatibility facade over the split SQLite repository modules."""

    pass
