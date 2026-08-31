#!/usr/bin/env python3
"""Docker liveness check. Never probes Telegram or WebDAV."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def check(*, require_ready: bool = False) -> tuple[bool, str]:
    heartbeat_path = Path(os.environ.get("HEALTH_HEARTBEAT_FILE", "session/runtime-health.json"))
    db_path = Path(os.environ.get("STATE_DB", "session/state.sqlite3"))
    download_root = Path(os.environ.get("DOWNLOAD_DIR", "/app/downloads"))
    max_age = max(10, _env_int("HEALTH_HEARTBEAT_MAX_AGE", 45))
    hard_min_free = max(0, _env_int("HEALTH_MIN_FREE_BYTES", 256 * 1024 * 1024))

    try:
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except Exception:
        return False, "heartbeat_missing"
    try:
        heartbeat_at = float(payload.get("heartbeat_at") or 0.0)
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return False, "heartbeat_invalid"
    if pid <= 0 or not Path(f"/proc/{pid}").exists():
        return False, "process_missing"
    if time.time() - heartbeat_at > max_age:
        return False, "heartbeat_stale"
    if require_ready and not bool(payload.get("ready")):
        return False, "not_ready"

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=2.0)
        try:
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
            if not row or str(row[0]).lower() != "ok":
                return False, "database_check_failed"
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        finally:
            conn.close()
    except Exception:
        return False, "database_unavailable"

    try:
        usage = shutil.disk_usage(download_root)
    except OSError:
        return False, "disk_unavailable"
    if int(usage.free) < hard_min_free:
        return False, "disk_critical"
    return True, "ok"


def main() -> int:
    ok, reason = check(require_ready=False)
    if not ok:
        print(reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
