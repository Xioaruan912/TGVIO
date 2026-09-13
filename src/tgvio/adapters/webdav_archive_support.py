from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
from pathlib import Path
import re
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
import uuid

from tgvio.domain.archive import (
    ArchiveCapabilities,
    ArchiveDeleteReceipt,
    ArchiveRemoteStat,
    ArchiveStoreReceipt,
)


class WebDavArchiveError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WebDavArchiveSafetyError(WebDavArchiveError):
    """The exact remote target no longer matches its durable receipt."""
