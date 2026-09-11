#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import sys


def _matches(row: dict[str, object], args: argparse.Namespace) -> bool:
    if args.job and str(row.get("job_id", "")) != args.job:
        return False
    if args.package and str(row.get("package_id", "")) != args.package:
        return False
    if args.event and not str(row.get("event", "")).startswith(args.event):
        return False
    if args.level and str(row.get("level", "")).upper() != args.level:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Query TGVIO structured operational logs")
    parser.add_argument("--file", default="logs/tgvio.jsonl")
    parser.add_argument("--job")
    parser.add_argument("--package")
    parser.add_argument("--event", help="event prefix, e.g. publish. or archive.object")
    parser.add_argument("--level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    parser.add_argument("--tail", type=int, default=100)
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        print(f"log file not found: {path}", file=sys.stderr)
        return 2

    rows: deque[dict[str, object]] = deque(maxlen=max(1, args.tail))
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and _matches(row, args):
                rows.append(row)

    for row in rows:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
