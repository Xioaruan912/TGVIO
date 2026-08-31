"""SQLite persistence primitives introduced in R2."""

from .sqlite import (
    BackupAttemptRecord,
    BackupFileRecord,
    InteractionSessionRecord,
    DestinationProfileRecord,
    JobItemRecord,
    JobClaim,
    JobEventRecord,
    JobRecord,
    PublishedMessageRecord,
    TransitionResult,
    MigrationChecksumError,
    MigrationError,
    RepositoryError,
    SQLiteRepository,
)

__all__ = [
    "BackupAttemptRecord",
    "BackupFileRecord",
    "InteractionSessionRecord",
    "DestinationProfileRecord",
    "JobItemRecord",
    "JobClaim",
    "PublishedMessageRecord",
    "TransitionResult",
    "JobEventRecord",
    "JobRecord",
    "MigrationChecksumError",
    "MigrationError",
    "RepositoryError",
    "SQLiteRepository",
]
