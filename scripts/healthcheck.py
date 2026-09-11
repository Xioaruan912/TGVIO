from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys


def check_health(db: Path, *, require_runtime: bool, max_age_seconds: int = 90) -> bool:
    if not db.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                return False
            if require_runtime:
                rows = conn.execute(
                    """
                    SELECT component, status,
                           (julianday('now') - julianday(updated_at)) * 86400.0 AS age_seconds
                    FROM runtime_health
                    WHERE component IN ('runtime', 'telegram')
                    """
                ).fetchall()
                states = {
                    str(component): (str(status), float(age_seconds))
                    for component, status, age_seconds in rows
                }
                runtime = states.get("runtime")
                telegram = states.get("telegram")
                if runtime is None or telegram is None:
                    return False
                if runtime[0] != "alive" or telegram[0] != "connected":
                    return False
                if runtime[1] < 0 or telegram[1] < 0:
                    return False
                if runtime[1] > max_age_seconds or telegram[1] > max_age_seconds:
                    return False
        finally:
            conn.close()
    except Exception:
        return False
    return True


def main() -> int:
    data_dir = Path(os.getenv("TGVIO_DATA_DIR", "/app/data"))
    db = data_dir / "state.sqlite3"
    require_runtime = os.getenv("TGVIO_RUN_BOT", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return 0 if check_health(db, require_runtime=require_runtime) else 1


if __name__ == "__main__":
    sys.exit(main())

