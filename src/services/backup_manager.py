"""R1 WebDAV facade around the legacy pipeline implementation."""

from __future__ import annotations

from typing import Any


class BackupManager:
    """Single handler-facing entry point for WebDAV state and operations.

    The protocol/lifecycle implementation remains on the legacy pipeline in R1
    so production behavior is unchanged. R2 can replace this adapter with a
    repository-backed implementation without changing Telegram handlers.
    """

    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline

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

    def logs_snapshot(self) -> dict:
        return {
            key: dict(value) if isinstance(value, dict) else value
            for key, value in self._pipeline.webdav_logs.items()
        }

