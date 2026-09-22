from __future__ import annotations

import random
from collections.abc import Sequence


def shuffled_cycle(media_ids: Sequence[str], *, rng: random.Random) -> list[str]:
    deck = list(media_ids)
    rng.shuffle(deck)
    return deck


def apply_recent_exclusion(
    deck: list[str], *, recent: Sequence[str], target: int
) -> list[str]:
    """Move recent media out of the next cycle's leading exclusion window.

    This is best effort: small catalogs cannot satisfy every exclusion without
    either duplicates or an infinite shuffle loop.
    """
    if target <= 0 or len(deck) < 2 or not recent:
        return deck
    blocked = set(recent)
    leading = min(target, len(deck))
    for index in range(leading):
        if deck[index] not in blocked:
            continue
        replacement = next(
            (candidate for candidate in range(leading, len(deck)) if deck[candidate] not in blocked),
            None,
        )
        if replacement is not None:
            deck[index], deck[replacement] = deck[replacement], deck[index]
    return deck
