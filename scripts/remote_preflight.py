#!/usr/bin/env python3
"""Read-only HostDZire production report used before and after cutover."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

from release_guard import GuardError, database_report, source_manifest


APP_ROOT = Path("/root/TGVIO")
RELEASE_ROOT = Path("/root/TGVIO-releases")
CURRENT_LINK = Path("/root/TGVIO-current")
CONTAINER = "tgvio"
KNOWN_SCHEMA_SQL_SHA256_BY_USER_VERSION = {
    0: "d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d",
    1: "593cccda96a2955990eb7807791682f73b67d4c8f0026062a37a2f7e785b25f6",
    2: "f5c9495e3947c109ee6598d7a7f8c0deac9d17d0fbb5c264d21de4f5d2bd4443",
    3: "74d331b0d2f112d354298ee7e10c39b2f16d4c58a0d0488eb198ebd912dff93b",
    4: "9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017",
    5: "c70f05023d89fb89127070a4cffb7f6232609b578eb9e47e4d20fc79afb879c2",
    6: "f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244",
    7: "9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90",
}


def _database_schema_is_known(database: dict[str, object]) -> bool:
    try:
        user_version = int(database.get("user_version", -1))
    except (TypeError, ValueError):
        return False
    expected = KNOWN_SCHEMA_SQL_SHA256_BY_USER_VERSION.get(user_version)
    if expected is None or database.get("schema_sql_sha256") != expected:
        return False
    ledger_present = bool(database.get("migration_ledger_present"))
    return (user_version == 0 and not ledger_present) or (user_version >= 1 and ledger_present)


def _run(args: list[str]) -> str:
    completed = subprocess.run(args, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _current_source() -> Path:
    if not CURRENT_LINK.exists() and not CURRENT_LINK.is_symlink():
        return APP_ROOT
    if not CURRENT_LINK.is_symlink():
        raise GuardError(f"{CURRENT_LINK} exists but is not a symbolic link")
    target = CURRENT_LINK.resolve(strict=True)
    release_root = RELEASE_ROOT.resolve()
    if target != APP_ROOT and release_root not in target.parents:
        raise GuardError("current source link escapes the release root")
    return target


def _allowed_source(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    release_root = RELEASE_ROOT.resolve()
    if resolved != APP_ROOT and release_root not in resolved.parents:
        raise GuardError("source root is outside the production/release roots")
    return resolved


def _container_report() -> dict[str, object]:
    ids = [
        line
        for line in _run(
            ["docker", "ps", "-aq", "--filter", f"name=^/{CONTAINER}$"]
        ).splitlines()
        if line
    ]
    if len(ids) != 1:
        return {
            "instances": len(ids),
            "status": "missing" if not ids else "ambiguous",
            "healthy": False,
        }
    raw = json.loads(_run(["docker", "inspect", CONTAINER]))[0]
    state = raw.get("State") or {}
    config = raw.get("Config") or {}
    labels = config.get("Labels") or {}
    app_commit = ""
    for entry in config.get("Env") or []:
        if entry.startswith("APP_COMMIT="):
            app_commit = entry.split("=", 1)[1]
            break
    health = state.get("Health") or {}
    container_manifest = _run(
        [
            "docker",
            "exec",
            CONTAINER,
            "sh",
            "-c",
            "cd /app && find src -type f -name '*.py' -print0 | "
            "LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1",
        ]
    )
    logs = subprocess.run(
        ["docker", "logs", "--since", "15m", CONTAINER],
        check=False,
        capture_output=True,
        text=True,
    )
    log_text = logs.stdout + logs.stderr
    error_markers = len(
        re.findall(r"traceback|fatal|unhandled|uncaught", log_text, flags=re.IGNORECASE)
    )
    return {
        "instances": 1,
        "id": str(raw.get("Id", "")),
        "image_id": str(raw.get("Image", "")),
        "app_commit": app_commit,
        "status": str(state.get("Status", "")),
        "health": str(health.get("Status", "none")),
        "healthy": state.get("Status") == "running" and health.get("Status") == "healthy",
        "restart_count": int(raw.get("RestartCount", 0)),
        "started_at": str(state.get("StartedAt", "")),
        "source_manifest": container_manifest,
        "compose_project": str(labels.get("com.docker.compose.project", "")),
        "compose_service": str(labels.get("com.docker.compose.service", "")),
        "compose_working_dir": str(labels.get("com.docker.compose.project.working_dir", "")),
        "bootstrap_markers": log_text.count("runtime.bootstrap.ready"),
        "telegram_ready_markers": log_text.count("runtime.telegram.ready"),
        "error_markers": error_markers,
    }


def report(source_root: Path | None = None) -> dict[str, object]:
    source = _allowed_source(source_root) if source_root else _current_source()
    container = _container_report()
    database = database_report(APP_ROOT / "data" / "state.sqlite3")
    release_commit_path = APP_ROOT / ".release-commit"
    release_commit = release_commit_path.read_text(encoding="utf-8").strip()
    release_id_path = APP_ROOT / ".release-id"
    release_id = release_id_path.read_text(encoding="utf-8").strip() if release_id_path.is_file() else ""
    env_path = APP_ROOT / ".env"
    env_mode = env_path.stat().st_mode & 0o777 if env_path.is_file() else 0
    env_secure = env_path.is_file() and env_path.stat().st_uid == 0 and env_mode & 0o077 == 0
    host_manifest = source_manifest(source)
    disk = shutil.disk_usage(RELEASE_ROOT.parent)
    try:
        ntp_synchronized = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    except (OSError, subprocess.CalledProcessError):
        ntp_synchronized = "unknown"
    blockers: list[str] = []
    if container.get("instances") != 1:
        blockers.append("container-count")
    if not container.get("healthy"):
        blockers.append("container-health")
    if container.get("restart_count") != 0:
        blockers.append("container-restarts")
    if container.get("error_markers") != 0:
        blockers.append("recent-log-errors")
    if container.get("compose_project") != "tgvio" or container.get("compose_service") != "tgvio":
        blockers.append("compose-identity")
    if container.get("app_commit") != release_commit:
        blockers.append("release-commit")
    if container.get("source_manifest") != host_manifest:
        blockers.append("source-manifest")
    if not database.get("safe_to_deploy"):
        blockers.append("database-activity-or-integrity")
    if not _database_schema_is_known(database):
        blockers.append("database-schema")
    if not env_secure:
        blockers.append("environment-permissions")
    return {
        "schema": "tgvio.production-report/v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hostname": os.uname().nodename,
        "ntp_synchronized": ntp_synchronized,
        "release_commit": release_commit,
        "release_id": release_id,
        "source_root": str(source),
        "source_manifest": host_manifest,
        "container": container,
        "database": database,
        "environment": {"present": env_path.is_file(), "mode": f"{env_mode:03o}", "secure": env_secure},
        "disk": {"free_bytes": disk.free, "total_bytes": disk.total},
        "blockers": blockers,
        "safe_to_deploy": not blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--require-safe", action="store_true")
    parser.add_argument("--expect-app-commit")
    parser.add_argument("--expect-source-manifest")
    parser.add_argument("--expect-image")
    args = parser.parse_args()
    try:
        result = report(args.source_root)
        problems: list[str] = []
        container = result["container"]
        assert isinstance(container, dict)
        if args.require_safe and not result["safe_to_deploy"]:
            problems.append("production preflight is not safe")
        if args.expect_app_commit and container.get("app_commit") != args.expect_app_commit:
            problems.append("APP_COMMIT mismatch")
        if args.expect_source_manifest:
            if result["source_manifest"] != args.expect_source_manifest:
                problems.append("host source manifest mismatch")
            if container.get("source_manifest") != args.expect_source_manifest:
                problems.append("container source manifest mismatch")
        if args.expect_image and container.get("image_id") != args.expect_image:
            problems.append("image mismatch")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if problems:
            print("preflight rejected: " + "; ".join(problems), file=sys.stderr)
            return 3
    except (GuardError, OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
