from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo


_BEIJING = ZoneInfo("Asia/Shanghai")


class VisibilityReason(StrEnum):
    DAILY_ROLLOVER = "daily_rollover"


class MaintenanceRunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"


class MaintenanceTargetPhase(StrEnum):
    PENDING = "pending"
    HIDE = "hide"
    STATUS = "status"
    DONE = "done"


class MaintenanceTargetState(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BusinessDay:
    day: str
    start_epoch: float


def business_day_bounds(now_epoch: float, *, hour: int = 6) -> BusinessDay:
    """Beijing business day: ``hour:00`` today through ``hour:00`` tomorrow."""

    now = datetime.fromtimestamp(float(now_epoch), tz=timezone.utc).astimezone(_BEIJING)
    start = now.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    if now < start:
        start = start - timedelta(days=1)
    return BusinessDay(day=start.strftime("%Y-%m-%d"), start_epoch=start.timestamp())
