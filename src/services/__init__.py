"""Application service facades introduced during the R1 refactor."""

from .backup_manager import BackupManager
from .interactions import InteractionSession, InteractionSessions
from .job_queue import ConfirmationTicket, JobQueue, RetryTicket
from .proxy_manager import ProxyManager
from .shadow_state import ShadowState
from .recovery import RecoveryAction, recover_jobs
from .operations import OperationStore, PendingOperation
from .network import NetworkCoordinator, NetworkSwitchResult
from .disk import DiskDecision, DiskManager, DiskSnapshot
from .stats import RuntimeStatsSnapshot, StatsService
from .health import RuntimeHeartbeat
from .dedup import ContentHash, DedupManager, sha256_file
from .media_compat import MediaCompatibilityManager
from .destination_profiles import DestinationProfileManager
from .source_profiles import SourceProfileManager

__all__ = [
    "BackupManager",
    "ConfirmationTicket",
    "InteractionSession",
    "InteractionSessions",
    "JobQueue",
    "ProxyManager",
    "ShadowState",
    "RecoveryAction",
    "recover_jobs",
    "RetryTicket",
    "OperationStore",
    "PendingOperation",
    "NetworkCoordinator",
    "NetworkSwitchResult",
    "DiskDecision",
    "DiskManager",
    "DiskSnapshot",
    "RuntimeStatsSnapshot",
    "StatsService",
    "RuntimeHeartbeat",
    "ContentHash",
    "DedupManager",
    "sha256_file",
    "MediaCompatibilityManager",
    "DestinationProfileManager",
    "SourceProfileManager",
]
