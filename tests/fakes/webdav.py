"""Deterministic synchronous fake for WebDAV lifecycle tests."""

import os
from typing import Any


class FakeBackupClient:
    def __init__(self) -> None:
        self.upload_calls: list[dict[str, Any]] = []
        self.remote_size_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.upload_result = True
        self.remote_sizes: dict[tuple[str, str], int | None] = {}
        self.delete_result = True

    def upload_file(
        self,
        base_url: str,
        remote_dir: str,
        local_path: str,
        user: str,
        passwd: str,
        retries: int = 2,
        remote_name: str = "",
        progress_callback=None,
    ) -> bool:
        call = {
            "base_url": base_url,
            "remote_dir": remote_dir,
            "local_path": local_path,
            "user": user,
            "passwd": passwd,
            "retries": retries,
            "remote_name": remote_name,
        }
        self.upload_calls.append(call)
        if progress_callback is not None and os.path.isfile(local_path):
            size = os.path.getsize(local_path)
            progress_callback(size, size)
        return self.upload_result

    def remote_file_size(
        self,
        base_url: str,
        remote_dir: str,
        filename: str,
        user: str,
        passwd: str,
    ) -> int | None:
        self.remote_size_calls.append(
            {
                "base_url": base_url,
                "remote_dir": remote_dir,
                "filename": filename,
                "user": user,
                "passwd": passwd,
            }
        )
        return self.remote_sizes.get((remote_dir, filename))

    def delete_remote(
        self,
        base_url: str,
        remote_dir: str,
        filename: str,
        user: str,
        passwd: str,
        retries: int = 2,
    ) -> bool:
        self.delete_calls.append(
            {
                "base_url": base_url,
                "remote_dir": remote_dir,
                "filename": filename,
                "user": user,
                "passwd": passwd,
                "retries": retries,
            }
        )
        return self.delete_result
