"""SQLite persistence primitives introduced in R2."""

from .sqlite import (
    BackupAttemptRecord,
    BackupFileRecord,
    InteractionSessionRecord,
    JobItemRecord,
    JobEventRecord,
    JobRecord,
    PublishedMessageRecord,
    MigrationChecksumError,
    MigrationError,
    RepositoryError,
    SQLiteRepository,
)

__all__ = [
    "BackupAttemptRecord",
    "BackupFileRecord",
    "InteractionSessionRecord",
    "JobItemRecord",
    "PublishedMessageRecord",
    "JobEventRecord",
    "JobRecord",
    "MigrationChecksumError",
    "MigrationError",
    "RepositoryError",
    "SQLiteRepository",
]
