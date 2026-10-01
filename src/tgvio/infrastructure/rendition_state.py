"""Separate maintenance checkpoints; never opens Bot or Player state."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import time

from tgvio.domain.renditions import RenditionTask


class RenditionState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS tasks(
            key TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0,
            error TEXT, written INTEGER NOT NULL DEFAULT 0)""")
        self.conn.commit()

    def discover(self, tasks: list[RenditionTask]) -> None:
        self.conn.executemany("INSERT OR IGNORE INTO tasks(key) VALUES(?)",
                              ((task.key,) for task in tasks))
        self.conn.commit()

    def eligible(self, task: RenditionTask, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        status, attempts, updated = self.conn.execute(
            "SELECT status,attempts,updated FROM tasks WHERE key=?", (task.key,)).fetchone()
        if status == "done":
            # Periodically verify remote objects again rather than trusting a local flag forever.
            return now - updated >= 86400
        return attempts < 5 and (attempts == 0 or now - updated >= 600)

    def finish(self, task: RenditionTask, written: int) -> None:
        self.conn.execute("UPDATE tasks SET status='done',attempts=0,updated=?,error=NULL,"
                          "written=written+? WHERE key=?", (time.time(), written, task.key))
        self.conn.commit()

    def fail(self, task: RenditionTask, error: Exception) -> None:
        self.conn.execute("UPDATE tasks SET status='failed',attempts=attempts+1,updated=?,"
                          "error=? WHERE key=?", (time.time(), type(error).__name__, task.key))
        self.conn.commit()

    def summary(self) -> dict:
        return {"tasks": self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "done": self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0],
                "failed": self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'").fetchone()[0],
                "blocked": self.conn.execute("SELECT COUNT(*) FROM tasks WHERE attempts>=5").fetchone()[0],
                "renditions_written": self.conn.execute("SELECT COALESCE(SUM(written),0) FROM tasks").fetchone()[0]}
