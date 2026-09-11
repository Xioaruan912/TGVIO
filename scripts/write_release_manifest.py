#!/usr/bin/env python3
"""Create the non-secret release manifest and deployment ledger atomically."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from release_guard import SOURCE_MANIFEST_ALGORITHM


def _load(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def build_manifest(args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    preflight = _load(args.preflight)
    postflight = _load(args.postflight)
    image_inspection = _load(args.image_inspection)
    backup = _load(args.backup)
    previous = _load(args.previous)
    assert preflight is not None and image_inspection is not None

    before_db = preflight.get("database")
    after_db = (postflight or preflight).get("database")
    if not isinstance(before_db, dict) or not isinstance(after_db, dict):
        raise ValueError("production reports are missing database facts")
    if args.migration == "none" and (
        before_db.get("user_version") != after_db.get("user_version")
        or before_db.get("schema_sql_sha256") != after_db.get("schema_sql_sha256")
    ):
        raise ValueError("schema changed during a migration-free release")

    generated_at = datetime.now(timezone.utc).isoformat()
    manifest: dict[str, object] = {
        "schema": "tgvio.release/v1",
        "status": args.status,
        "release_id": args.release_id,
        "git_commit": args.git_commit,
        "git_ref_verified": "origin/main",
        "git_archive_sha256": args.git_archive_sha256,
        "source_manifest_algorithm": SOURCE_MANIFEST_ALGORITHM,
        "source_manifest": args.source_manifest,
        "requirements_lock_sha256": args.requirements_lock_sha256,
        "dockerfile_sha256": args.dockerfile_sha256,
        "build": {
            "created_at": generated_at,
            "platform": "linux/amd64",
            "base_image": args.base_image,
            "test_image_id": args.test_image_id,
            "runtime_image_id": args.runtime_image_id,
            "runtime_tag": f"tgvio-release:{args.release_id}",
        },
        "gates": {
            "unittest": {
                "tests": args.tests,
                "seconds": args.test_seconds,
                "status": "passed",
                "network": "none",
                "bot_started": False,
            },
            "compileall": "passed",
            "architecture": "passed",
            "source_and_secret_guard": "passed",
            "compose_placeholder_config": "passed",
            "runtime_image_inspection": image_inspection,
        },
        "database": {
            "migration": args.migration,
            "user_version_before": before_db.get("user_version"),
            "user_version_after": after_db.get("user_version"),
            "schema_sql_sha256_before": before_db.get("schema_sql_sha256"),
            "schema_sql_sha256_after": after_db.get("schema_sql_sha256"),
        },
        "deployment": {
            "preflight": preflight,
            "postflight": postflight,
            "backup": backup,
            "previous": previous,
        },
    }
    ledger: dict[str, object] = {
        "schema": "tgvio.deployment-ledger/v1",
        "status": args.status,
        "release_id": args.release_id,
        "git_commit": args.git_commit,
        "runtime_image_id": args.runtime_image_id,
        "source_manifest": args.source_manifest,
        "preflight": preflight,
        "backup": backup,
        "previous": previous,
        "postflight": postflight,
        "updated_at": generated_at,
    }
    return manifest, ledger


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest-output", type=Path, required=True)
    result.add_argument("--ledger-output", type=Path, required=True)
    result.add_argument("--status", choices=("ready", "deployed", "rolled_back"), required=True)
    result.add_argument("--release-id", required=True)
    result.add_argument("--git-commit", required=True)
    result.add_argument("--git-archive-sha256", required=True)
    result.add_argument("--source-manifest", required=True)
    result.add_argument("--requirements-lock-sha256", required=True)
    result.add_argument("--dockerfile-sha256", required=True)
    result.add_argument("--base-image", required=True)
    result.add_argument("--test-image-id", required=True)
    result.add_argument("--runtime-image-id", required=True)
    result.add_argument("--tests", type=int, required=True)
    result.add_argument("--test-seconds", type=float, required=True)
    result.add_argument("--migration", default="none")
    result.add_argument("--preflight", type=Path, required=True)
    result.add_argument("--postflight", type=Path)
    result.add_argument("--image-inspection", type=Path, required=True)
    result.add_argument("--backup", type=Path)
    result.add_argument("--previous", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    manifest, ledger = build_manifest(args)
    _atomic_json(args.manifest_output, manifest)
    _atomic_json(args.ledger_output, ledger)
    print(
        json.dumps(
            {
                "status": args.status,
                "manifest": str(args.manifest_output),
                "ledger": str(args.ledger_output),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
