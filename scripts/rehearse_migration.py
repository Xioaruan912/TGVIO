#!/usr/bin/env python3
"""Rehearse TGVIO migrations against an offline SQLite database copy.

This tool mutates the supplied copy in place. It deliberately refuses the known
production database paths and emits only schema/version/count facts; it never
prints job identifiers, users, message text, media paths, or other business data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from tgvio.infrastructure.migration_runner import MigrationRunner
from tgvio.infrastructure.schema import TGVIO_BASELINE_SCHEMA_SQL_SHA256, schema_sql_sha256


_KNOWN_SCHEMA_SQL_SHA256_BY_USER_VERSION = {
    0: TGVIO_BASELINE_SCHEMA_SQL_SHA256,
    1: "593cccda96a2955990eb7807791682f73b67d4c8f0026062a37a2f7e785b25f6",
    2: "f5c9495e3947c109ee6598d7a7f8c0deac9d17d0fbb5c264d21de4f5d2bd4443",
    3: "74d331b0d2f112d354298ee7e10c39b2f16d4c58a0d0488eb198ebd912dff93b",
    4: "9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017",
    5: "c70f05023d89fb89127070a4cffb7f6232609b578eb9e47e4d20fc79afb879c2",
    6: "f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244",
    7: "9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90",
    8: "5b80e9d9aa0abe258883e5eb8b26d54ce2481b4e03daa3ee45f75e477423f76a",
}


_BLOCKED_DATABASES = {
    Path("/root/TGVIO/data/state.sqlite3"),
    Path("/app/data/state.sqlite3"),
}


class RehearsalError(RuntimeError):
    pass


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _state_counts(connection: sqlite3.Connection, table: str) -> dict[str, int]:
    return {
        str(state): int(count)
        for state, count in connection.execute(
            f'SELECT state, COUNT(*) FROM "{table}" GROUP BY state ORDER BY state'
        )
    }


def _database_facts(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        tables = _table_names(connection)
        facts: dict[str, object] = {
            "quick_check": "ok" if quick == ["ok"] else ";".join(quick),
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            "schema_sql_sha256": schema_sql_sha256(connection),
            "migration_ledger_present": "schema_migrations" in tables,
            "table_count": len(tables),
        }
        if "jobs" in tables:
            facts["jobs"] = {
                "total": int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]),
                "states": _state_counts(connection, "jobs"),
            }
        if "publish_steps" in tables:
            facts["publish_steps"] = {
                "total": int(connection.execute("SELECT COUNT(*) FROM publish_steps").fetchone()[0]),
                "states": _state_counts(connection, "publish_steps"),
            }
        if "archive_packages" in tables:
            facts["archive_packages"] = {
                "total": int(connection.execute("SELECT COUNT(*) FROM archive_packages").fetchone()[0]),
                "states": _state_counts(connection, "archive_packages"),
            }
        if "archive_objects" in tables:
            facts["archive_objects"] = {
                "total": int(connection.execute("SELECT COUNT(*) FROM archive_objects").fetchone()[0]),
                "states": _state_counts(connection, "archive_objects"),
            }
        if "job_progress" in tables:
            facts["job_progress"] = {
                "total": int(connection.execute("SELECT COUNT(*) FROM job_progress").fetchone()[0]),
            }
        return facts
    finally:
        connection.close()


def _business_counts(facts: dict[str, object]) -> dict[str, object]:
    return {
        key: facts[key]
        for key in (
            "jobs",
            "publish_steps",
            "archive_packages",
            "archive_objects",
            "job_progress",
        )
        if key in facts
    }


def rehearse(database_copy: Path, backup_dir: Path) -> dict[str, object]:
    database_input = database_copy.expanduser()
    if database_input.is_symlink():
        raise RehearsalError("database copy must not be a symbolic link")
    database_copy = database_input.resolve()
    backup_dir = backup_dir.expanduser().resolve()
    if database_copy in _BLOCKED_DATABASES:
        raise RehearsalError("refusing to run migration rehearsal against the production database path")
    if not database_copy.is_file():
        raise RehearsalError("database copy must be a regular existing file")
    if backup_dir == database_copy.parent and backup_dir.name in {"data", "TGVIO"}:
        raise RehearsalError("backup directory is too close to a production-style data path")
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise RehearsalError("backup directory must be empty before rehearsal")

    before = _database_facts(database_copy)
    if before["quick_check"] != "ok":
        raise RehearsalError("database copy failed quick_check before rehearsal")
    from_version = int(before["user_version"])
    expected_before_hash = _KNOWN_SCHEMA_SQL_SHA256_BY_USER_VERSION.get(from_version)
    if expected_before_hash is None or before["schema_sql_sha256"] != expected_before_hash:
        raise RehearsalError("database copy schema hash is not a known TGVIO release schema")
    if from_version == 0 and before["migration_ledger_present"]:
        raise RehearsalError("user_version=0 must not have a migration ledger")
    if from_version >= 1 and not before["migration_ledger_present"]:
        raise RehearsalError("migrated database copy is missing the migration ledger")

    first = MigrationRunner(database_copy, backup_dir=backup_dir).run()
    if from_version >= first.latest_version:
        raise RehearsalError("database copy is already at the latest migration version")
    after = _database_facts(database_copy)
    if after["quick_check"] != "ok":
        raise RehearsalError("database copy failed quick_check after takeover")
    if _business_counts(after) != _business_counts(before):
        raise RehearsalError("business row/state counts changed during baseline takeover")
    if not after["migration_ledger_present"]:
        raise RehearsalError("migration ledger is missing after takeover")
    if after["user_version"] != first.latest_version:
        raise RehearsalError("user_version does not match the migration runner after takeover")
    if not first.backup_created:
        raise RehearsalError("first takeover did not create a SQLite backup")

    second = MigrationRunner(database_copy, backup_dir=backup_dir).run()
    repeated = _database_facts(database_copy)
    if second.applied_now:
        raise RehearsalError("repeat migration run was not a no-op")
    if second.backup_created:
        raise RehearsalError("repeat no-op unexpectedly created another backup")
    if repeated != after:
        raise RehearsalError("database facts changed during repeat no-op")

    backup_files = sorted(backup_dir.glob("*.sqlite3"))
    if len(backup_files) != 1:
        raise RehearsalError("expected exactly one pre-migration backup")
    backup_facts = _database_facts(backup_files[0])
    if backup_facts["quick_check"] != "ok":
        raise RehearsalError("pre-migration backup failed quick_check")
    if backup_facts["user_version"] != before["user_version"]:
        raise RehearsalError("pre-migration backup does not preserve user_version")
    if backup_facts["migration_ledger_present"] != before["migration_ledger_present"]:
        raise RehearsalError("pre-migration backup does not preserve ledger presence")
    if backup_facts["schema_sql_sha256"] != before["schema_sql_sha256"]:
        raise RehearsalError("pre-migration backup does not preserve schema identity")
    if _business_counts(backup_facts) != _business_counts(before):
        raise RehearsalError("pre-migration backup does not preserve business counts")

    return {
        "status": "passed",
        "migration": {
            "from_version": from_version,
            "to_version": first.latest_version,
            "applied_now": list(first.applied_now),
        },
        "before": before,
        "after": after,
        "repeat_noop": {
            "applied_now": list(second.applied_now),
            "backup_created": second.backup_created,
        },
        "backup": backup_facts,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("database_copy", type=Path, help="offline SQLite copy to mutate during rehearsal")
    result.add_argument(
        "--backup-dir",
        type=Path,
        required=True,
        help="empty directory dedicated to rehearsal backups",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        report = rehearse(args.database_copy, args.backup_dir)
    except (OSError, sqlite3.DatabaseError, RehearsalError, RuntimeError) as exc:
        print(f"migration rehearsal failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
