"""How long a watched mark keeps a video out of "unseen"."""
from __future__ import annotations

# The choices the settings offer, in days; None keeps every mark for good.
FORGET_CHOICES: tuple[int | None, ...] = (None, 7, 15, 30, 60, 90, 180)


def parse_forget_after_days(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value not in FORGET_CHOICES:
        raise ValueError("invalid forget_after_days")
    return value
