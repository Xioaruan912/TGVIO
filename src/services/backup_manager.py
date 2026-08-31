"""R1 WebDAV facade around the legacy pipeline implementation."""

from __future__ import annotations

import asyncio
from typing import Any

from .. import webdav


class BackupManager:
    """Single handler-facing entry point for WebDAV state and operations.

    The protocol/lifecycle implementation remains on the legacy pipeline in R1
    so production behavior is unchanged. R2 can replace this adapter with a
    repository-backed implementation without changing Telegram handlers.
    """

    def __init__(self, pipeline: Any, shadow: Any | None = None) -> None:
        self._pipeline = pipeline
        self._shadow = shadow

    def config(self) -> dict:
        return self._pipeline.webdav_cfg

    def config_snapshot(self) -> dict:
        return dict(self._pipeline.webdav_cfg)

    def get_config(self, field: str, default: Any = None) -> Any:
        return self._pipeline.webdav_cfg.get(field, default)

    def set_config(self, field: str, value: Any) -> None:
        self._pipeline.webdav_cfg[field] = value
        self._pipeline._save_webdav_cfg()

    def set_enabled(self, enabled: bool) -> None:
        self.set_config("enabled", bool(enabled))

    async def retry(self, key: str) -> str:
        return await self._pipeline._webdav_retry(key)

    async def delete(self, key: str) -> str:
        return await self._pipeline._webdav_delete(key)

    async def upload_cache(self, seq: int, user_id: int = 0) -> str:
        return await self._pipeline._webdav_upload_cache(seq, user_id)

    async def autoretry_once(self) -> None:
        await self._pipeline._webdav_autoretry_once()

    async def test_connection(self) -> webdav.WebDavProbeResult:
        """Run the explicit read-only WebDAV connectivity/quota probe."""
        cfg = self.config_snapshot()
        return await asyncio.to_thread(
            webdav.probe_connection,
            str(cfg.get("url") or ""),
            str(cfg.get("path") or ""),
            str(cfg.get("user") or ""),
            str(cfg.get("pass") or ""),
        )

    async def test_write(self) -> webdav.WebDavWriteProbeResult:
        """Run the explicitly confirmed write/verify/delete WebDAV probe."""
        cfg = self.config_snapshot()
        return await asyncio.to_thread(
            webdav.probe_write,
            str(cfg.get("url") or ""),
            str(cfg.get("path") or ""),
            str(cfg.get("user") or ""),
            str(cfg.get("pass") or ""),
        )

    def logs_snapshot(self) -> dict:
        return {
            key: dict(value) if isinstance(value, dict) else value
            for key, value in self._pipeline.webdav_logs.items()
        }

    def shadow_attempt_started(self, seq: int, remote_dir: str, files: list[dict]) -> None:
        if self._shadow is not None:
            self._shadow.backup_started(seq, remote_dir, files)

    async def ensure_file(self, seq: int, remote_dir: str, file: dict):
        if self._shadow is None:
            return None, None
        return await self._shadow.ensure_backup_file(seq, remote_dir, file)

    async def update_file(self, file_id: int, **kwargs) -> None:
        if self._shadow is not None:
            await self._shadow.update_backup_file(file_id, **kwargs)

    async def update_attempt(self, attempt_id: int, **kwargs) -> None:
        if self._shadow is not None:
            await self._shadow.update_backup_attempt(attempt_id, **kwargs)

    async def retry_due(self, seq: int, remote_dir: str) -> tuple[bool, float | None]:
        if self._shadow is None:
            return True, None
        return await self._shadow.backup_retry_due(seq, remote_dir)

