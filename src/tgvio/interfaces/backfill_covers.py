"""Independent bounded cover worker. Never starts Telegram or opens Bot/Player DB."""
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

from tgvio.adapters.cover_archive import CoverArchivePort, CoverDiscovery
from tgvio.application.cover_backfill_runner import CommittedCoverBackfill
from tgvio.infrastructure.rendition_state import MaintenanceState


def report(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def priority_order(path: str | None) -> dict[str, int]:
    if not path:
        return {}
    file = Path(path)
    if file.is_symlink() or file.stat().st_size > 64 * 1024:
        raise ValueError("cover priority file exceeds budget")
    value = json.loads(file.read_text())
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("invalid cover priorities")
    result = {}
    for digest in value:
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid priority identity")
        result.setdefault(digest, len(result))
    return result


async def work(args) -> None:
    root = Path(args.work_dir)
    root.mkdir(parents=True, exist_ok=True)
    state = MaintenanceState(root / "progress.sqlite3", written_label="covers_written", persistent_retry=True)
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
        priority = priority_order(args.priority_file)
        port = CoverArchivePort(
            os.environ["TGVIO_ARCHIVE_WEBDAV_URL"], os.environ["TGVIO_ARCHIVE_WEBDAV_USER"],
            os.environ["TGVIO_ARCHIVE_WEBDAV_PASSWORD"], timeout=15,
            response_timeout=30, verify_attempts=6, verify_interval_seconds=2)
        discovery = CoverDiscovery(port, os.environ.get("TGVIO_ARCHIVE_REMOTE_ROOT", "TGVIO"))
        runner = CommittedCoverBackfill(port)
        tasks = []
        scan_at = -600.0
        loop = asyncio.get_running_loop()
        while True:
            if not tasks or loop.time() - scan_at >= 600:
                try:
                    discovered = await discovery.tasks()
                    discovered.sort(key=lambda t: (priority.get(t.media["sha256"], 1001),
                                                   int(t.media.get("size_bytes") or 0), t.key))
                    tasks = discovered
                    state.discover(tasks)
                    scan_at = loop.time()
                    report("scan", **state.summary())
                except Exception as error:
                    report("scan_failed", error=type(error).__name__, **state.summary())
                    if not args.watch:
                        raise
                    if not tasks:
                        await asyncio.sleep(30)
                        continue
                    # Known task bindings are revalidated by the runner before writes.
                    scan_at = loop.time() - 570
            if args.dry_run:
                report("plan", eligible=sum(state.eligible(t) for t in tasks),
                       source_bytes=sum(int(t.media["size_bytes"]) for t in tasks),
                       max_sample_bytes=12*1024**2, download_rate_bytes=512*1024)
                return
            processed = 0
            batch = state.ready_batch(tasks, limit=args.limit or (10 if args.watch else 0))
            for task in batch:
                if shutil.disk_usage(root).free < 64*1024**2:
                    report("disk_budget_wait", **state.summary())
                    break
                try:
                    with tempfile.TemporaryDirectory(prefix="sample-", dir=root) as directory:
                        written = await runner.run(task, Path(directory))
                    state.finish(task, written)
                    report("complete", task=task.key[:12], written=written, **state.summary())
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    state.fail(task, error)
                    report("failed", task=task.key[:12], error=type(error).__name__, **state.summary())
                processed += 1
                if args.limit and processed >= args.limit:
                    break
                await asyncio.sleep(1)
            if not args.watch:
                report("finished", **state.summary())
                return
            await asyncio.sleep(10 if batch else 60)
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
    parser.add_argument("--priority-file")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("limit must be non-negative")
    try:
        asyncio.run(cancellable(args))
    except asyncio.CancelledError:
        report("stopped")


if __name__ == "__main__":
    main()
