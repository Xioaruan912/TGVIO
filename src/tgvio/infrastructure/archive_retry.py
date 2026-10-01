"""Bounded transient Archive I/O retry shared by maintenance transports."""
from __future__ import annotations

import asyncio
import http.client


async def retry_archive(operation):
    for attempt in range(3):
        try:
            return await operation()
        except (OSError, http.client.HTTPException, RuntimeError):
            if attempt == 2:
                raise
            await asyncio.sleep(attempt + 1)
