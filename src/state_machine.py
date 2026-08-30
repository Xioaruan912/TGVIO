"""Explicit durable job state rules introduced by R3-A."""

from __future__ import annotations

from dataclasses import dataclass


class InvalidTransition(ValueError):
    """A requested durable job transition is not allowed."""


TERMINAL_STATES = frozenset({"succeeded", "cancelled"})
PAUSABLE_STATES = frozenset({"queued", "downloading", "ready"})
RESUMABLE_STATES = PAUSABLE_STATES

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "collecting": frozenset({"awaiting_confirmation", "queued", "cancelled", "failed"}),
    "awaiting_confirmation": frozenset({"queued", "cancelled", "failed"}),
    "queued": frozenset({"downloading", "paused", "cancelled", "failed"}),
    "downloading": frozenset({"ready", "paused", "cancelled", "failed"}),
    "ready": frozenset({"publishing", "paused", "cancelled", "failed"}),
    "publishing": frozenset({"succeeded", "cancelled", "failed"}),
    "paused": RESUMABLE_STATES,
    "failed": frozenset({"queued", "ready"}),
    "succeeded": frozenset(),
    "cancelled": frozenset(),
}


@dataclass(frozen=True)
class TransitionPlan:
    from_state: str
    to_state: str
    resume_state: str | None


def plan_transition(
    from_state: str,
    to_state: str,
    *,
    current_resume_state: str | None = None,
) -> TransitionPlan:
    """Validate one transition and derive the persisted resume_state value."""
    if from_state not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(f"unknown job state: {from_state}")
    if to_state not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(f"unknown job state: {to_state}")
    if to_state not in ALLOWED_TRANSITIONS[from_state]:
        raise InvalidTransition(f"transition not allowed: {from_state} -> {to_state}")

    if to_state == "paused":
        if from_state not in PAUSABLE_STATES:
            raise InvalidTransition(f"state cannot be paused: {from_state}")
        resume_state = from_state
    elif from_state == "paused":
        if current_resume_state not in RESUMABLE_STATES:
            raise InvalidTransition("paused job has invalid resume_state")
        if to_state != current_resume_state:
            raise InvalidTransition(
                f"paused job must resume to {current_resume_state}, not {to_state}"
            )
        resume_state = None
    else:
        resume_state = None
    return TransitionPlan(from_state, to_state, resume_state)

