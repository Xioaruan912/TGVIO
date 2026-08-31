"""R1 WebDAV facade around the legacy pipeline implementation."""

from __future__ import annotations

import asyncio
import os
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

    @property
    def repository(self):
        return getattr(self._pipeline, "repository", None)

    async def attempt_page(self, page: int, *, page_size: int = 5) -> dict[str, Any]:
        repo = self.repository
        if repo is None:
            return {"page": 0, "pages": 1, "total": 0, "items": []}
        total = await repo.count_backup_attempts()
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(0, int(page)), pages - 1)
        items = await repo.list_backup_attempt_page(limit=page_size, offset=page * page_size)
        return {"page": page, "pages": pages, "total": total, "items": items}

    async def attempt_detail_page(self, attempt_id: int, page: int, *, page_size: int = 5) -> dict[str, Any] | None:
        repo = self.repository
        if repo is None:
            return None
        attempt = await repo.backup_attempt_detail(int(attempt_id))
        if attempt is None:
            return None
        total = await repo.count_backup_files(int(attempt_id))
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(0, int(page)), pages - 1)
        files = await repo.list_backup_file_page(int(attempt_id), limit=page_size, offset=page * page_size)
        return {"attempt": attempt, "page": page, "pages": pages, "total": total, "files": files}

    async def retry_file(self, file_id: int) -> str:
        repo = self.repository
        if repo is None:
            return "持久化备份状态不可用"
        row = await repo.backup_file_detail(int(file_id))
        if row is None:
            return "备份文件不存在或已过期"
        seq = row.get("legacy_seq")
        if seq is None:
            return "任务缺少可执行队列编号"
        local_path = str(row.get("local_path") or "")
        if not local_path or not os.path.isfile(local_path):
            await repo.update_backup_file_status(
                int(file_id), state="failed", error_code="cache_missing",
                error_message="备份所需的本地缓存不存在",
            )
            return "本地缓存不存在，无法重试"
        file_state = {
            "local": local_path,
            "name": str(row.get("remote_name") or ""),
            "status": str(row.get("state") or "failed"),
            "size": int(row.get("size_bytes") or 0),
            "retry_count": 0,
        }
        ok = await self._pipeline._webdav_transfer_file(
            seq=int(seq), remote_dir=str(row.get("remote_dir") or ""),
            file=file_state, cfg=self.config_snapshot(),
        )
        await self._finalize_attempt_from_repository(int(row["attempt_id"]))
        return "✅ 文件重试成功" if ok else "❌ 文件重试失败，已记录错误"

    async def retry_attempt(self, attempt_id: int) -> str:
        repo = self.repository
        if repo is None:
            return "持久化备份状态不可用"
        failed_ids = await repo.backup_retry_file_ids(int(attempt_id))
        if not failed_ids:
            return "没有需要重试的文件"
        succeeded = 0
        for file_id in failed_ids:
            result = await self.retry_file(file_id)
            if result.startswith("✅"):
                succeeded += 1
        return f"🔄 重试完成：成功 {succeeded}/{len(failed_ids)}"

    async def _finalize_attempt_from_repository(self, attempt_id: int) -> None:
        repo = self.repository
        if repo is None:
            return
        summary = await repo.backup_attempt_file_summary(int(attempt_id))
        if int(summary.get("total") or 0) == 0:
            return
        if int(summary.get("failed") or 0) > 0:
            await repo.update_backup_attempt_status(
                int(attempt_id), state="failed", error_code=summary.get("error_code"),
                error_message=summary.get("error_message"), finished=True,
            )
        else:
            await repo.update_backup_attempt_status(
                int(attempt_id), state="succeeded", error_code=None,
                error_message=None, finished=True,
            )

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

