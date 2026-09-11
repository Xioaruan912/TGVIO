from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JobProgress:
    job_id: str
    phase: str
    current: int = 0
    total: int = 0
    item_index: int | None = None
    item_total: int = 0
    detail_code: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.current < 0 or self.total < 0 or self.item_total < 0:
            raise ValueError("progress values must be non-negative")
        if self.item_index is not None and self.item_index < 0:
            raise ValueError("progress item index must be non-negative")
