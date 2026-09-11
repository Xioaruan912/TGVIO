from __future__ import annotations

import asyncio
from collections import deque
import json
from pathlib import Path
from typing import Iterable


_SAFE_FIELDS = {
    "ts",
    "level",
    "component",
    "event",
    "message",
    "job_id",
    "plan_id",
    "package_id",
    "state",
    "error_code",
    "exception_type",
    "step_index",
    "step_kind",
    "target",
    "object_index",
    "item_index",
    "item_count",
    "step_count",
    "receipt_count",
    "confirmed_effects",
    "expected_effects",
    "duration_ms",
    "reused_remote",
    "verification_method",
}


class JsonlOperationalLogReader:
    """Read already-redacted structured operational logs for diagnostics."""

    def __init__(self, log_dir: Path, *, filename: str = "tgvio.jsonl") -> None:
        self._log_dir = Path(log_dir)
        self._filename = filename

    async def recent_for_job(
        self,
        job_id: str,
        *,
        plan_id: str | None = None,
        package_id: str | None = None,
        limit: int = 40,
    ) -> list[dict[str, object]]:
        return await asyncio.to_thread(
            self._recent_for_job_sync,
            job_id,
            plan_id,
            package_id,
            limit,
        )

    def _recent_for_job_sync(
        self,
        job_id: str,
        plan_id: str | None,
        package_id: str | None,
        limit: int,
    ) -> list[dict[str, object]]:
        rows: deque[dict[str, object]] = deque(maxlen=max(1, min(int(limit), 200)))
        identifiers = {str(job_id)}
        if plan_id:
            identifiers.add(str(plan_id))
        if package_id:
            identifiers.add(str(package_id))

        for path in self._paths_oldest_first():
            try:
                handle = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if not self._matches(row, identifiers):
                        continue
                    rows.append(
                        {
                            key: value
                            for key, value in row.items()
                            if key in _SAFE_FIELDS and self._scalar(value)
                        }
                    )
        return list(rows)

    def _paths_oldest_first(self) -> Iterable[Path]:
        base = self._log_dir / self._filename
        backups: list[tuple[int, Path]] = []
        try:
            candidates = self._log_dir.glob(f"{self._filename}.*")
        except OSError:
            candidates = ()
        for path in candidates:
            suffix = path.name.removeprefix(f"{self._filename}.")
            if suffix.isdigit() and path.is_file():
                backups.append((int(suffix), path))
        # RotatingFileHandler: .5 is older than .1; current file is newest.
        for _number, path in sorted(backups, key=lambda pair: pair[0], reverse=True):
            yield path
        if base.is_file():
            yield base

    @staticmethod
    def _matches(row: dict[str, object], identifiers: set[str]) -> bool:
        for field in ("job_id", "plan_id", "package_id"):
            value = row.get(field)
            if value is not None and str(value) in identifiers:
                return True
        return False

    @staticmethod
    def _scalar(value: object) -> bool:
        return value is None or isinstance(value, (str, int, float, bool))

