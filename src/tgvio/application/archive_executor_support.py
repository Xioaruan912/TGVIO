from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
import time

from tgvio.application.archive_capabilities import (
    record_archive_probe_failure,
    record_archive_probe_success,
)
from tgvio.application.ports import ArchiveTransport, JobRepository
from tgvio.domain.archive import (
    ArchiveCapabilities,
    ArchiveObject,
    ArchiveObjectState,
    ArchivePackage,
    ArchivePackageState,
    archive_complete_marker,
    archive_json_bytes,
    archive_json_sha256,
)
from tgvio.observability import log_event


class ArchiveCapabilityError(RuntimeError):
    pass


class ArchiveExecutionError(RuntimeError):
    pass
