#!/usr/bin/env python3
"""Build one release from a pushed commit and deliver it to HostDZire."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

from release_guard import (
    GuardError,
    git_source_manifest,
    sha256_file,
    source_manifest,
    verify_archive,
    verify_repository,
    verify_tree,
)


HOST = "199.47.242.40"
PORT = 22
USER = "root"
HOST_ALIAS = "HostDZire"
EXPECTED_HOST_FINGERPRINT = "SHA256:1QFIKfh+MeSYTGuU/8PsGjlGfDDzVrJXqT+ilmSfqAw"
DEFAULT_KEY = Path("/root/.ssh/tgvio_hostdzire_ed25519")
RELEASE_ROOT = "/root/TGVIO-releases"


class DeployError(RuntimeError):
    pass


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        input=input_text,
        capture_output=capture,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ssh_base(repo: Path, key: Path) -> list[str]:
    known_hosts = repo / "deploy" / "hostdzire_known_hosts"
    return [
        "ssh",
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        f"HostKeyAlias={HOST_ALIAS}",
        "-p",
        str(PORT),
        f"{USER}@{HOST}",
    ]


def _scp_base(repo: Path, key: Path) -> list[str]:
    known_hosts = repo / "deploy" / "hostdzire_known_hosts"
    return [
        "scp",
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        f"HostKeyAlias={HOST_ALIAS}",
        "-P",
        str(PORT),
    ]


def _verify_ssh_material(repo: Path, key: Path) -> None:
    known_hosts = repo / "deploy" / "hostdzire_known_hosts"
    if not key.is_file() or key.is_symlink():
        raise DeployError(f"dedicated deployment key is missing: {key}")
    key_mode = stat.S_IMODE(key.stat().st_mode)
    if key_mode & 0o077:
        raise DeployError("dedicated deployment key permissions are too broad")
    if not known_hosts.is_file() or known_hosts.is_symlink():
        raise DeployError("pinned HostDZire known_hosts file is missing")
    lines = [
        line.strip()
        for line in known_hosts.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1 or not lines[0].startswith(f"{HOST_ALIAS} ssh-ed25519 "):
        raise DeployError("HostDZire known_hosts must contain exactly one ED25519 key")
    fingerprint = _run(["ssh-keygen", "-lf", str(known_hosts)], capture=True).stdout
    if EXPECTED_HOST_FINGERPRINT not in fingerprint:
        raise DeployError("HostDZire host-key fingerprint is not the audited fingerprint")


def _git_archive(repo: Path, commit: str, output: Path) -> None:
    _run(
        [
            "git",
            "archive",
            "--format=tar.gz",
            f"--output={output}",
            commit,
        ],
        cwd=repo,
    )


def _remote_json(command: list[str], remote_command: str) -> dict[str, object]:
    output = _run([*command, remote_command], capture=True).stdout.strip()
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeployError("HostDZire returned an invalid preflight report") from exc
    if not isinstance(value, dict):
        raise DeployError("HostDZire preflight report is not an object")
    return value


def _verify_control_preflight(repo: Path, report: dict[str, object], head: str) -> tuple[str, str]:
    if not report.get("safe_to_deploy"):
        blockers = report.get("blockers")
        raise DeployError(f"HostDZire preflight is blocked: {blockers}")
    container = report.get("container")
    if not isinstance(container, dict):
        raise DeployError("HostDZire report is missing container facts")
    current_commit = str(report.get("release_commit", ""))
    current_manifest = str(report.get("source_manifest", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", current_commit):
        raise DeployError("production release commit is not a full Git commit")
    if container.get("app_commit") != current_commit:
        raise DeployError("production APP_COMMIT differs from release metadata")
    expected_current_manifest = git_source_manifest(repo, current_commit)
    if current_manifest != expected_current_manifest:
        raise DeployError("production source differs from its recorded Git commit")
    if container.get("source_manifest") != expected_current_manifest:
        raise DeployError("container source differs from its recorded Git commit")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", current_commit, head],
        cwd=repo,
        check=False,
    )
    if ancestor.returncode != 0:
        raise DeployError("production commit is not an ancestor of the release commit")
    created_at = datetime.fromisoformat(str(report["created_at"]))
    skew = abs((datetime.now(timezone.utc) - created_at).total_seconds())
    if skew > 300:
        raise DeployError(f"HostDZire clock skew exceeds five minutes ({skew:.0f}s)")
    return current_commit, current_manifest


def deploy(args: argparse.Namespace) -> int:
    repo = _repo_root()
    key = args.key.expanduser().resolve()
    _verify_ssh_material(repo, key)
    verify_tree(repo)
    repository = verify_repository(
        repo,
        require_clean=True,
        require_pushed=True,
        verify_remote=True,
    )
    if repository["branch"] != "main" or repository["upstream"] != "origin/main":
        raise DeployError("production release must come from local main tracking origin/main")
    _run(["git", "diff", "--check", "HEAD^", "HEAD"], cwd=repo)
    head = str(repository["head"])
    expected_source_manifest = source_manifest(repo)
    if git_source_manifest(repo, head) != expected_source_manifest:
        raise DeployError("working source manifest differs from the pushed commit")
    lock_sha = sha256_file(repo / "requirements.lock")
    dockerfile_sha = sha256_file(repo / "Dockerfile")
    phase = args.phase.lower()
    if not re.fullmatch(r"r2-[0-9]{2}", phase):
        raise DeployError("phase must match R2-NN")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release_id = f"{phase}-{head[:7]}-{timestamp}"
    if not re.fullmatch(r"r2-[0-9]{2}-[0-9a-f]{7}-[0-9]{8}T[0-9]{6}Z", release_id):
        raise DeployError("generated release id is invalid")

    ssh = _ssh_base(repo, key)
    scp = _scp_base(repo, key)
    with tempfile.TemporaryDirectory(prefix="tgvio-release-") as temporary:
        archive = Path(temporary) / "source.tar.gz"
        _git_archive(repo, head, archive)
        archive_report = verify_archive(archive)
        archive_sha = str(archive_report["sha256"])
        remote_dir = f"{RELEASE_ROOT}/{release_id}"
        quoted_remote_dir = shlex.quote(remote_dir)
        bootstrap = (
            "set -eu; umask 077; "
            f"release_dir={quoted_remote_dir}; "
            'test ! -e "$release_dir"; '
            'mkdir -p "$release_dir/incoming" "$release_dir/source" "$release_dir/evidence"'
        )
        _run([*ssh, bootstrap])
        _run([*scp, str(archive), f"{USER}@{HOST}:{remote_dir}/incoming/source.tar.gz"])
        extract = (
            "set -eu; umask 077; "
            f"release_dir={quoted_remote_dir}; expected={shlex.quote(archive_sha)}; "
            'actual=$(sha256sum "$release_dir/incoming/source.tar.gz" | cut -d" " -f1); '
            'test "$actual" = "$expected"; '
            'tar -xzf "$release_dir/incoming/source.tar.gz" -C "$release_dir/source"'
        )
        _run([*ssh, extract])

    preflight_command = (
        f"python3 {shlex.quote(remote_dir + '/source/scripts/remote_preflight.py')} | "
        f"tee {shlex.quote(remote_dir + '/evidence/preflight-control.json')}"
    )
    control_report = _remote_json(ssh, preflight_command)
    current_commit, current_manifest = _verify_control_preflight(repo, control_report, head)

    remote_release = remote_dir + "/source/scripts/remote_release.sh"
    remote_args = [
        remote_release,
        "deploy",
        release_id,
        head,
        expected_source_manifest,
        archive_sha,
        lock_sha,
        dockerfile_sha,
        current_commit,
        current_manifest,
    ]
    remote_command = " ".join(shlex.quote(value) for value in remote_args)
    print(f"release_start={release_id}", flush=True)
    _run([*ssh, remote_command])
    print(f"release_complete={release_id}", flush=True)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--phase", default="R2-02", help="release phase, e.g. R2-02")
    result.add_argument(
        "--key",
        type=Path,
        default=Path(os.getenv("TGVIO_DEPLOY_KEY", str(DEFAULT_KEY))),
        help="path to the dedicated HostDZire private key",
    )
    return result


def main() -> int:
    try:
        return deploy(parser().parse_args())
    except (DeployError, GuardError, OSError, subprocess.CalledProcessError) as exc:
        print(f"deployment failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
