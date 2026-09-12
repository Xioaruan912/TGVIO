#!/usr/bin/env python3
"""Fail-closed source, Git, architecture, and SQLite release gates."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
from typing import Iterable


SOURCE_MANIFEST_ALGORITHM = (
    "find src -type f -name '*.py' -print0 | LC_ALL=C sort -z | "
    "xargs -0 sha256sum | sha256sum | cut -d' ' -f1"
)

_SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_RUNTIME_ROOTS = {"data", "downloads", "session", "logs"}
_PRIVATE_KEY_NAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "private-key-material",
        re.compile(br"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
    ),
    ("github-token", re.compile(br"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    (
        "telegram-bot-token",
        re.compile(br"(?<![A-Za-z0-9])\d{7,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9])"),
    ),
    (
        "bearer-credential",
        re.compile(br"(?i)\bBearer[ \t]+[A-Za-z0-9._~-]{20,}"),
    ),
)
_MAX_SECRET_SCAN_BYTES = 5 * 1024 * 1024


class GuardError(RuntimeError):
    pass


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=text,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _forbidden_path_reason(relative: PurePosixPath) -> str | None:
    if relative.is_absolute() or not relative.parts:
        return "absolute-or-empty-path"
    if any(part in {"", ".", ".."} for part in relative.parts):
        return "unsafe-path-component"
    if relative.parts[0] in _RUNTIME_ROOTS:
        return "runtime-volume"
    name = relative.name
    lower = name.lower()
    if lower == ".env" or (lower.startswith(".env.") and lower != ".env.example"):
        return "environment-secret-file"
    if lower in {".release.env", ".netrc"}:
        return "credential-or-release-environment"
    if lower in _PRIVATE_KEY_NAMES:
        return "private-key-path"
    if lower.endswith((".pem", ".key", ".p12", ".pfx", ".session", ".session-journal")):
        return "credential-or-session-path"
    if lower.endswith((".sqlite", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal")):
        return "runtime-database"
    return None


def _secret_findings(data: bytes) -> list[str]:
    if len(data) > _MAX_SECRET_SCAN_BYTES or b"\x00" in data:
        return []
    return [name for name, pattern in _SECRET_PATTERNS if pattern.search(data)]


def verify_tree(root: Path) -> dict[str, object]:
    root = root.resolve()
    if not root.is_dir():
        raise GuardError(f"source root is not a directory: {root}")
    files = 0
    scanned = 0
    problems: list[str] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if relative.parts and relative.parts[0] in _SKIP_DIRS:
                continue
            reason = _forbidden_path_reason(relative)
            if reason:
                problems.append(f"{relative}: {reason}")
                continue
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                problems.append(f"{relative}: symbolic-link")
            elif stat.S_ISDIR(mode):
                if entry.name not in _SKIP_DIRS:
                    stack.append(path)
            elif stat.S_ISREG(mode):
                files += 1
                if entry.stat(follow_symlinks=False).st_size <= _MAX_SECRET_SCAN_BYTES:
                    findings = _secret_findings(path.read_bytes())
                    scanned += 1
                    for finding in findings:
                        problems.append(f"{relative}: {finding}")
            else:
                problems.append(f"{relative}: non-regular-file")
    if problems:
        raise GuardError("source tree rejected:\n" + "\n".join(sorted(problems)))
    return {"status": "passed", "files": files, "secret_scanned_files": scanned}


def verify_archive(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise GuardError(f"archive does not exist: {path}")
    files = 0
    scanned = 0
    problems: list[str] = []
    with tarfile.open(path, "r:*") as archive:
        for member in archive.getmembers():
            if member.isdir() and member.name in {".", "./"}:
                continue
            relative = PurePosixPath(member.name)
            reason = _forbidden_path_reason(relative)
            if reason:
                problems.append(f"{member.name}: {reason}")
                continue
            if member.isdir():
                continue
            if not member.isreg():
                problems.append(f"{member.name}: non-regular-archive-member")
                continue
            files += 1
            if member.size <= _MAX_SECRET_SCAN_BYTES:
                handle = archive.extractfile(member)
                if handle is None:
                    problems.append(f"{member.name}: unreadable-member")
                    continue
                findings = _secret_findings(handle.read())
                scanned += 1
                for finding in findings:
                    problems.append(f"{member.name}: {finding}")
    if problems:
        raise GuardError("source archive rejected:\n" + "\n".join(sorted(problems)))
    return {
        "status": "passed",
        "files": files,
        "secret_scanned_files": scanned,
        "sha256": sha256_file(path),
    }


def _manifest_from_items(items: Iterable[tuple[str, bytes]]) -> str:
    combined = hashlib.sha256()
    count = 0
    for relative, content in sorted(items, key=lambda item: item[0].encode("utf-8")):
        combined.update(hashlib.sha256(content).hexdigest().encode("ascii"))
        combined.update(b"  ")
        combined.update(relative.encode("utf-8"))
        combined.update(b"\n")
        count += 1
    if count == 0:
        raise GuardError("no Python source files found below src/")
    return combined.hexdigest()


def source_manifest(root: Path) -> str:
    root = root.resolve()
    source = root / "src"
    if not source.is_dir():
        raise GuardError(f"missing source directory: {source}")
    items = (
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in source.rglob("*.py")
        if path.is_file() and not path.is_symlink()
    )
    return _manifest_from_items(items)


def git_source_manifest(repo: Path, commit: str) -> str:
    repo = repo.resolve()
    commit = str(_run(["git", "rev-parse", "--verify", f"{commit}^{{commit}}"], cwd=repo).stdout).strip()
    listing = _run(
        ["git", "ls-tree", "-rz", "--name-only", commit, "--", "src"],
        cwd=repo,
        text=False,
    ).stdout
    assert isinstance(listing, bytes)
    names = [item.decode("utf-8") for item in listing.split(b"\0") if item and item.endswith(b".py")]
    items: list[tuple[str, bytes]] = []
    for name in names:
        content = _run(["git", "show", f"{commit}:{name}"], cwd=repo, text=False).stdout
        assert isinstance(content, bytes)
        items.append((name, content))
    return _manifest_from_items(items)


def check_architecture(root: Path) -> dict[str, object]:
    source = root.resolve() / "src" / "tgvio"
    if not source.is_dir():
        raise GuardError(f"missing package root: {source}")
    problems: list[str] = []
    checked = 0
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(root.resolve()).as_posix()
        module_parts = path.relative_to(source).parts
        layer = module_parts[0] if len(module_parts) > 1 else "root"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        for imported in imports:
            if not imported.startswith("tgvio."):
                continue
            imported_layer = imported.split(".", 2)[1]
            if imported == "tgvio.main" and path.name != "main.py":
                problems.append(f"{relative}: imports composition root")
            if layer == "domain" and imported_layer != "domain":
                problems.append(f"{relative}: domain imports {imported}")
            elif layer == "application" and imported_layer not in {
                "application",
                "domain",
                "observability",
            }:
                problems.append(f"{relative}: application imports {imported}")
            elif layer == "infrastructure" and imported_layer not in {
                "infrastructure",
                "domain",
                "observability",
            }:
                problems.append(f"{relative}: infrastructure imports {imported}")
        checked += 1
    if problems:
        raise GuardError("architecture boundary rejected:\n" + "\n".join(problems))
    return {"status": "passed", "python_files": checked}


def verify_repository(
    root: Path,
    *,
    require_clean: bool,
    require_pushed: bool,
    verify_remote: bool,
) -> dict[str, object]:
    root = root.resolve()
    head = str(_run(["git", "rev-parse", "HEAD"], cwd=root).stdout).strip()
    branch = str(_run(["git", "branch", "--show-current"], cwd=root).stdout).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise GuardError("HEAD did not resolve to a full Git commit")
    if require_clean:
        dirty = str(
            _run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=root,
            ).stdout
        ).strip()
        if dirty:
            raise GuardError("working tree is dirty")
    upstream = ""
    upstream_commit = ""
    if require_pushed or verify_remote:
        upstream = str(
            _run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=root).stdout
        ).strip()
        upstream_commit = str(_run(["git", "rev-parse", "@{upstream}"], cwd=root).stdout).strip()
        if require_pushed and head != upstream_commit:
            raise GuardError(f"HEAD is not the pushed upstream commit ({upstream})")
    remote_commit = ""
    if verify_remote:
        if not upstream.startswith("origin/"):
            raise GuardError("release upstream must be origin/<branch>")
        remote_ref = "refs/heads/" + upstream.split("/", 1)[1]
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        output = str(_run(["git", "ls-remote", "origin", remote_ref], cwd=root, env=env).stdout).strip()
        fields = output.split()
        if len(fields) != 2 or fields[1] != remote_ref:
            raise GuardError(f"unable to verify remote ref {remote_ref}")
        remote_commit = fields[0]
        if remote_commit != head:
            raise GuardError("HEAD is not the current remote commit")
    return {
        "status": "passed",
        "head": head,
        "branch": branch,
        "upstream": upstream,
        "upstream_commit": upstream_commit,
        "remote_commit": remote_commit,
    }


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }


def _scalar(connection: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = connection.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def database_report(path: Path) -> dict[str, object]:
    path = path.resolve()
    if not path.is_file():
        raise GuardError(f"database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        quick_rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        quick_check = "ok" if quick_rows == ["ok"] else ";".join(quick_rows)
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema_sql = "".join(
            f"{row[0]}\n"
            for row in connection.execute(
                "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type, name"
            )
        )
        schema_sha256 = hashlib.sha256(schema_sql.encode("utf-8")).hexdigest()
        tables = _table_names(connection)
        terminal = ("succeeded", "failed", "cancelled")
        jobs_total = _scalar(connection, "SELECT COUNT(*) FROM jobs") if "jobs" in tables else 0
        jobs_active = (
            _scalar(
                connection,
                "SELECT COUNT(*) FROM jobs WHERE state NOT IN (?,?,?)",
                terminal,
            )
            if "jobs" in tables
            else 0
        )
        progress_active = (
            _scalar(
                connection,
                """
                SELECT COUNT(*) FROM job_progress p
                JOIN jobs j ON j.id=p.job_id
                WHERE j.state NOT IN (?,?,?)
                """,
                terminal,
            )
            if {"jobs", "job_progress"}.issubset(tables)
            else 0
        )
        publish_active = (
            _scalar(
                connection,
                """
                SELECT COUNT(*) FROM publish_steps s
                JOIN publish_plans p ON p.id=s.plan_id
                JOIN jobs j ON j.id=p.job_id
                WHERE j.state NOT IN (?,?,?) AND s.state IN ('pending','running')
                """,
                terminal,
            )
            if {"jobs", "publish_plans", "publish_steps"}.issubset(tables)
            else 0
        )
        publish_uncertain_or_partial = (
            _scalar(
                connection,
                """
                SELECT COUNT(*) FROM jobs
                WHERE error_code IN ('publish_partial','publish_uncertain')
                """,
            )
            if "jobs" in tables
            else 0
        )
        publish_uncommitted_effects = (
            _scalar(
                connection,
                """
                SELECT COUNT(*) FROM publish_effects e
                WHERE e.effect_type != 'publish_step_receipts_committed'
                  AND NOT EXISTS (
                    SELECT 1 FROM publish_effects marker
                    WHERE marker.plan_id=e.plan_id
                      AND marker.step_index=e.step_index
                      AND marker.effect_type='publish_step_receipts_committed'
                  )
                """,
            )
            if "publish_effects" in tables
            else 0
        )
        archive_packages_active = (
            _scalar(
                connection,
                "SELECT COUNT(*) FROM archive_packages WHERE state IN ('planned','staging','uploading','verifying')",
            )
            if "archive_packages" in tables
            else 0
        )
        archive_objects_active = (
            _scalar(
                connection,
                """
                SELECT COUNT(*) FROM archive_objects o
                JOIN archive_packages p ON p.id=o.package_id
                WHERE p.state IN ('planned','staging','uploading','verifying')
                  AND o.state IN ('pending','uploading','verifying')
                """,
            )
            if {"archive_packages", "archive_objects"}.issubset(tables)
            else 0
        )
        phase_claims_active = (
            _scalar(
                connection,
                """
                SELECT COUNT(*) FROM job_phase_claims
                WHERE expires_at > CAST(strftime('%s','now') AS INTEGER)
                """,
            )
            if "job_phase_claims" in tables
            else 0
        )
        runtime_leases_active = (
            _scalar(
                connection,
                """
                SELECT COUNT(*) FROM runtime_leases
                WHERE expires_at > CAST(strftime('%s','now') AS INTEGER)
                """,
            )
            if "runtime_leases" in tables
            else 0
        )
        runtime_lease_conflicts = (
            _scalar(
                connection,
                """
                SELECT COUNT(*) FROM runtime_leases
                WHERE expires_at > CAST(strftime('%s','now') AS INTEGER)
                  AND lease_name != 'telegram-runtime'
                """,
            )
            if "runtime_leases" in tables
            else 0
        )
        blockers = {
            "jobs": jobs_active,
            "progress": progress_active,
            "publish": publish_active,
            "publish_uncertain_or_partial": publish_uncertain_or_partial,
            "publish_uncommitted_effects": publish_uncommitted_effects,
            "archive_packages": archive_packages_active,
            "archive_objects": archive_objects_active,
            "claims_or_leases": phase_claims_active,
            "runtime_lease_conflicts": runtime_lease_conflicts,
        }
        return {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "quick_check": quick_check,
            "user_version": user_version,
            "schema_sql_sha256": schema_sha256,
            "migration_ledger_present": "schema_migrations" in tables,
            "jobs_total": jobs_total,
            "runtime_leases_active": runtime_leases_active,
            "blocking": blockers,
            "safe_to_deploy": quick_check == "ok" and not any(blockers.values()),
        }
    finally:
        connection.close()


def sqlite_backup(source: Path, destination: Path) -> dict[str, object]:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise GuardError(f"database does not exist: {source}")
    if destination.exists():
        raise GuardError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()
    destination.chmod(0o600)
    report = database_report(destination)
    if report["quick_check"] != "ok":
        raise GuardError("SQLite backup failed quick_check")
    return {**report, "sha256": sha256_file(destination)}


def sqlite_restore(backup: Path, destination: Path) -> dict[str, object]:
    backup = backup.resolve()
    destination = destination.resolve()
    if not backup.is_file() or not destination.is_file():
        raise GuardError("SQLite restore requires existing backup and destination files")
    backup_report = database_report(backup)
    if backup_report["quick_check"] != "ok":
        raise GuardError("SQLite restore source failed quick_check")
    source_conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True, timeout=10)
    destination_conn = sqlite3.connect(destination, timeout=10)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()
    destination.chmod(0o600)
    report = database_report(destination)
    if report["quick_check"] != "ok":
        raise GuardError("restored SQLite database failed quick_check")
    return {**report, "sha256": sha256_file(destination)}


def _json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    tree = sub.add_parser("verify-tree")
    tree.add_argument("root", type=Path, nargs="?", default=Path("."))

    archive = sub.add_parser("verify-archive")
    archive.add_argument("archive", type=Path)

    manifest = sub.add_parser("source-manifest")
    manifest.add_argument("root", type=Path, nargs="?", default=Path("."))

    git_manifest = sub.add_parser("git-source-manifest")
    git_manifest.add_argument("commit")
    git_manifest.add_argument("--repo", type=Path, default=Path("."))

    architecture = sub.add_parser("architecture")
    architecture.add_argument("root", type=Path, nargs="?", default=Path("."))

    repository = sub.add_parser("repository")
    repository.add_argument("root", type=Path, nargs="?", default=Path("."))
    repository.add_argument("--require-clean", action="store_true")
    repository.add_argument("--require-pushed", action="store_true")
    repository.add_argument("--verify-remote", action="store_true")

    database = sub.add_parser("db-report")
    database.add_argument("database", type=Path)
    database.add_argument("--require-safe", action="store_true")

    backup = sub.add_parser("sqlite-backup")
    backup.add_argument("source", type=Path)
    backup.add_argument("destination", type=Path)

    restore = sub.add_parser("sqlite-restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("destination", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-tree":
            _json(verify_tree(args.root))
        elif args.command == "verify-archive":
            _json(verify_archive(args.archive))
        elif args.command == "source-manifest":
            print(source_manifest(args.root))
        elif args.command == "git-source-manifest":
            print(git_source_manifest(args.repo, args.commit))
        elif args.command == "architecture":
            _json(check_architecture(args.root))
        elif args.command == "repository":
            _json(
                verify_repository(
                    args.root,
                    require_clean=args.require_clean,
                    require_pushed=args.require_pushed,
                    verify_remote=args.verify_remote,
                )
            )
        elif args.command == "db-report":
            report = database_report(args.database)
            _json(report)
            if args.require_safe and not report["safe_to_deploy"]:
                raise GuardError("database has active work or failed quick_check")
        elif args.command == "sqlite-backup":
            _json(sqlite_backup(args.source, args.destination))
        elif args.command == "sqlite-restore":
            _json(sqlite_restore(args.backup, args.destination))
        else:
            raise GuardError(f"unsupported command: {args.command}")
    except (GuardError, OSError, sqlite3.Error, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"release guard failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
