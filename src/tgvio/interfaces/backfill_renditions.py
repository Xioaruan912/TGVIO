"""Single maintenance worker, no Telegram identity or Bot database access."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile

from tgvio.adapters.rendition_archive import RenditionArchivePort
from tgvio.adapters.rendition_discovery import RenditionDiscovery
from tgvio.application.rendition_backfill import RenditionBackfill
from tgvio.infrastructure.rendition_state import RenditionState


def report(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


async def work(args) -> None:
    root = Path(args.work_dir)
    root.mkdir(parents=True, exist_ok=True)
    state = RenditionState(root / "progress.sqlite3", persistent_retry=True)
    lock = None
    try:
        if args.status:
            report("status", **state.summary())
            return
        lock = (root / "worker.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.retry_failed:
            state.conn.execute("UPDATE tasks SET attempts=0,status='pending' WHERE status='failed'")
            state.conn.commit()
        port = RenditionArchivePort(
            os.environ["TGVIO_ARCHIVE_WEBDAV_URL"], os.environ["TGVIO_ARCHIVE_WEBDAV_USER"],
            os.environ["TGVIO_ARCHIVE_WEBDAV_PASSWORD"], timeout=30,
            response_timeout=180, verify_attempts=6, verify_interval_seconds=5)
        discovery = RenditionDiscovery(port, os.environ.get("TGVIO_ARCHIVE_REMOTE_ROOT", "TGVIO"))
        runner = RenditionBackfill(port)
        while True:
            try:
                tasks = await discovery.tasks()
            except Exception as error:
                report("scan_failed", error=type(error).__name__, **state.summary())
                if not args.watch:
                    raise
                await asyncio.sleep(30)
                continue
            state.discover(tasks)
            report("scan", **state.summary())
            if args.dry_run:
                report("plan", eligible=sum(state.eligible(t) for t in tasks),
                       requiring_480=sum(480 in t.required for t in tasks),
                       requiring_720=sum(720 in t.required for t in tasks),
                       total_source_bytes=sum(int(t.media["size_bytes"]) for t in tasks))
                return
            processed = 0
            batch = args.limit or (10 if args.watch else 0)
            for task in state.ready_batch(tasks, limit=batch):
                try:
                    size = int(task.media["size_bytes"])
                    if shutil.disk_usage(root).free < size * 2 + 2 * 1024**3:
                        report("disk_budget_wait", **state.summary())
                        break
                    report("start", task=task.key[:12], source_bytes=size, heights=task.required)
                    with tempfile.TemporaryDirectory(prefix="encode-", dir=root) as directory:
                        written = await runner.run(task, Path(directory))
                    state.finish(task, written)
                    report("complete", task=task.key[:12], written=written, **state.summary())
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    state.fail(task, error)
                    report("failed", task=task.key[:12], error=type(error).__name__, **state.summary())
                processed += 1
                if batch and processed >= batch:
                    break
                await asyncio.sleep(2)
            if not args.watch:
                report("finished", **state.summary())
                return
            await asyncio.sleep(60)
    finally:
        state.conn.close()
        if lock is not None:
            lock.close()


async def cancellable(args):
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, current.cancel)
    await work(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default="/work")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    try:
        asyncio.run(cancellable(args))
    except asyncio.CancelledError:
        report("stopped")


if __name__ == "__main__":
    main()
