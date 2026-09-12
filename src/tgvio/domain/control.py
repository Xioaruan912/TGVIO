from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JobControlState:
    job_id: str
    cancel_requested: bool = False
    cancel_reason: str | None = None
    retry_count: int = 0
    hold_requested: bool = False
    hold_reason: str | None = None
    hold_revision: int = 0


@dataclass(frozen=True, slots=True)
class QueueControlState:
    paused: bool = False
    pause_reason: str | None = None
    revision: int = 0
