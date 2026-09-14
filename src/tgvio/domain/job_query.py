from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tgvio.domain.job import Job


class JobListFilter(StrEnum):
    ALL = "all"
    ACTIVE = "active"
    HELD = "held"
    FAILED = "failed"
    COMPLETED = "completed"
    TODAY = "today"
    PENDING = "pending"
    HISTORY = "history"


@dataclass(frozen=True, slots=True)
class JobListEntry:
    job: Job
    held: bool = False
    accepted_order: int | None = None
    display_no: int | None = None
    display_business_day: str | None = None

    @property
    def label_number(self) -> int | None:
        """Display number for the current business day, falling back to FIFO order."""
        return self.display_no if self.display_no is not None else self.accepted_order


@dataclass(frozen=True, slots=True)
class JobPage:
    entries: tuple[JobListEntry, ...]
    filter: JobListFilter
    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        if self.total <= 0:
            return 1
        return (self.total + self.page_size - 1) // self.page_size


@dataclass(frozen=True, slots=True)
class FailureSummary:
    job: Job
    job_actionable: bool
    archive_actionable: bool
    archive_package_id: str | None = None
    archive_error_code: str | None = None
    accepted_order: int | None = None
    display_no: int | None = None
    display_business_day: str | None = None

    @property
    def label_number(self) -> int | None:
        return self.display_no if self.display_no is not None else self.accepted_order


@dataclass(frozen=True, slots=True)
class FailurePage:
    entries: tuple[FailureSummary, ...]
    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        if self.total <= 0:
            return 1
        return (self.total + self.page_size - 1) // self.page_size
