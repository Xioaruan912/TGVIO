from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from typing import Any


_MIGRATION_TABLE = "schema_migrations"
_WHITESPACE_RE = re.compile(r"\s+")
TGVIO_BASELINE_SCHEMA_SQL_SHA256 = "d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d"


@dataclass(frozen=True)
class SchemaIdentity:
    fingerprint: str
    snapshot: dict[str, object]


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalized_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return _WHITESPACE_RE.sub(" ", value.strip())


def _table_columns(connection: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    rows = connection.execute(f"PRAGMA table_xinfo({_quote_identifier(table)})").fetchall()
    return [
        {
            "cid": int(row[0]),
            "name": str(row[1]),
            "type": str(row[2]),
            "notnull": int(row[3]),
            "default": row[4],
            "pk": int(row[5]),
            "hidden": int(row[6]),
        }
        for row in rows
    ]


def _table_foreign_keys(connection: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    rows = connection.execute(f"PRAGMA foreign_key_list({_quote_identifier(table)})").fetchall()
    result = [
        {
            "id": int(row[0]),
            "seq": int(row[1]),
            "table": str(row[2]),
            "from": str(row[3]),
            "to": None if row[4] is None else str(row[4]),
            "on_update": str(row[5]),
            "on_delete": str(row[6]),
            "match": str(row[7]),
        }
        for row in rows
    ]
    return sorted(result, key=lambda row: (int(row["id"]), int(row["seq"])))


def _index_columns(connection: sqlite3.Connection, index_name: str) -> list[dict[str, object]]:
    rows = connection.execute(f"PRAGMA index_xinfo({_quote_identifier(index_name)})").fetchall()
    return [
        {
            "seqno": int(row[0]),
            "cid": int(row[1]),
            "name": None if row[2] is None else str(row[2]),
            "desc": int(row[3]),
            "coll": None if row[4] is None else str(row[4]),
            "key": int(row[5]),
        }
        for row in rows
    ]


def _table_indexes(connection: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    rows = connection.execute(f"PRAGMA index_list({_quote_identifier(table)})").fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        name = str(row[1])
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        result.append(
            {
                "name": name,
                "unique": int(row[2]),
                "origin": str(row[3]),
                "partial": int(row[4]),
                "sql": _normalized_sql(sql_row[0] if sql_row else None),
                "columns": _index_columns(connection, name),
            }
        )
    return sorted(result, key=lambda item: str(item["name"]))


def schema_snapshot(
    connection: sqlite3.Connection,
    *,
    include_migration_ledger: bool = False,
) -> dict[str, object]:
    objects = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE type IN ('table', 'view', 'trigger')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    tables: list[dict[str, object]] = []
    views: list[dict[str, object]] = []
    triggers: list[dict[str, object]] = []
    for obj_type, name, table_name, sql in objects:
        name = str(name)
        if not include_migration_ledger and name == _MIGRATION_TABLE:
            continue
        if obj_type == "table":
            tables.append(
                {
                    "name": name,
                    "sql": _normalized_sql(sql),
                    "columns": _table_columns(connection, name),
                    "foreign_keys": _table_foreign_keys(connection, name),
                    "indexes": _table_indexes(connection, name),
                }
            )
        elif obj_type == "view":
            views.append(
                {
                    "name": name,
                    "sql": _normalized_sql(sql),
                }
            )
        elif obj_type == "trigger":
            triggers.append(
                {
                    "name": name,
                    "table": str(table_name),
                    "sql": _normalized_sql(sql),
                }
            )
    return {
        "tables": tables,
        "views": views,
        "triggers": triggers,
    }


def schema_fingerprint(snapshot: dict[str, object]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def schema_sql_sha256(connection: sqlite3.Connection) -> str:
    schema_sql = "".join(
        f"{row[0]}\n"
        for row in connection.execute(
            "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type, name"
        )
    )
    return hashlib.sha256(schema_sql.encode("utf-8")).hexdigest()


def schema_identity(
    connection: sqlite3.Connection,
    *,
    include_migration_ledger: bool = False,
) -> SchemaIdentity:
    snapshot = schema_snapshot(
        connection,
        include_migration_ledger=include_migration_ledger,
    )
    return SchemaIdentity(
        fingerprint=schema_fingerprint(snapshot),
        snapshot=snapshot,
    )


def snapshot_diff(expected: dict[str, object], actual: dict[str, object]) -> dict[str, Any]:
    expected_tables = {str(item["name"]): item for item in expected.get("tables", [])}  # type: ignore[index]
    actual_tables = {str(item["name"]): item for item in actual.get("tables", [])}  # type: ignore[index]
    expected_names = set(expected_tables)
    actual_names = set(actual_tables)
    changed = sorted(
        name
        for name in expected_names & actual_names
        if expected_tables[name] != actual_tables[name]
    )
    return {
        "missing_tables": sorted(expected_names - actual_names),
        "extra_tables": sorted(actual_names - expected_names),
        "changed_tables": changed,
        "views_changed": expected.get("views", []) != actual.get("views", []),
        "triggers_changed": expected.get("triggers", []) != actual.get("triggers", []),
    }
