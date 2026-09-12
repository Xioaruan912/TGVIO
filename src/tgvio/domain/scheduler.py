from __future__ import annotations

from dataclasses import dataclass

from tgvio.domain.job import JobState


@dataclass(frozen=True, slots=True)
class RuntimeLease:
    lease_name: str
    holder_id: str
    generation: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class PhaseClaim:
    job_id: str
    phase: str
    holder_id: str
    generation: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class PublishGate:
    job_id: str
    accepted_order: int
    state: JobState
    error_code: str | None

    @property
    def blocks_for_manual_review(self) -> bool:
        return self.state == JobState.FAILED and self.error_code in {
            "publish_partial",
            "publish_uncertain",
        }
