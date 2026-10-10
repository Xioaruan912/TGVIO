"""Separate maintenance checkpoints; never opens Bot or Player state."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import time

from tgvio.domain.renditions import RenditionTask


class MaintenanceState:
    def __init__(self, path: Path, *, written_label: str = "renditions_written",
                 persistent_retry: bool = False) -> None:
        self.written_label = written_label
        self.persistent_retry = persistent_retry
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
        if status in ("done", "gone"):
            # Periodically verify remote objects again rather than trusting a local flag
            # forever; a source that came back is picked up the same way.
            return now - updated >= 86400
        if attempts >= 5 and not self.persistent_retry:
            return False
        delay = 600 if attempts < 5 else min(21600, 3600 * 2**min(attempts-5, 3))
        return attempts == 0 or now - updated >= delay

    def ready_batch(self, tasks: list[RenditionTask], *, limit: int = 0,
                    now: float | None = None) -> list[RenditionTask]:
        """Oldest due retries get reserved slots; fresh work keeps its order."""
        now = time.time() if now is None else now
        ready = [t for t in tasks if self.eligible(t, now)]
        checkpoints = {key: (status, updated) for key, status, updated in
                       self.conn.execute("SELECT key,status,updated FROM tasks")}
        # A long encode batch can outlast cooldowns. Input order alone would
        # repeatedly select the same failed prefix and starve later failures.
        retries = sorted((t for t in ready if checkpoints[t.key][0] == "failed"),
                         key=lambda t: checkpoints[t.key][1])
        # Work never done comes before the daily re-check of finished tasks: a full
        # re-check round (over a thousand tasks) otherwise fills every batch for most
        # of a day while new sources wait.
        fresh = ([t for t in ready if checkpoints[t.key][0] == "pending"]
                 + [t for t in ready if checkpoints[t.key][0] in ("done", "gone")])
        if not limit:
            return retries + fresh
        quota = max(1, limit//3)
        selected = retries[:quota] + fresh[:limit-min(quota, len(retries))]
        keys = {t.key for t in selected}
        return selected + [t for t in retries + fresh if t.key not in keys][:limit-len(selected)]

    def finish(self, task: RenditionTask, written: int) -> None:
        self.conn.execute("UPDATE tasks SET status='done',attempts=0,updated=?,error=NULL,"
                          "written=written+? WHERE key=?", (time.time(), written, task.key))
        self.conn.commit()

    def gone(self, task: RenditionTask) -> None:
        """The source is no longer in the archive: neither done nor a failure to retry."""
        self.conn.execute("UPDATE tasks SET status='gone',attempts=0,updated=?,error='SourceGone'"
                          " WHERE key=?", (time.time(), task.key))
        self.conn.commit()

    def fail(self, task: RenditionTask, error: Exception) -> None:
        self.conn.execute("UPDATE tasks SET status='failed',attempts=attempts+1,updated=?,"
                          "error=? WHERE key=?", (time.time(), type(error).__name__, task.key))
        self.conn.commit()

    def summary(self) -> dict:
        return {"tasks": self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "done": self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0],
                "failed": self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'").fetchone()[0],
                "gone": self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='gone'").fetchone()[0],
                "blocked": 0 if self.persistent_retry else self.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE attempts>=5").fetchone()[0],
                "slow_retry": self.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='failed' AND attempts>=5").fetchone()[0]
                    if self.persistent_retry else 0,
                self.written_label: self.conn.execute("SELECT COALESCE(SUM(written),0) FROM tasks").fetchone()[0]}


# Existing rendition callers retain their public name; one checkpoint implementation.
RenditionState = MaintenanceState
