"""A collection the user made, as one value the layers can pass around.

Favourites live in their own table with their own backup chain, so the
"favorites" collection the API exposes is a projection the adapter adds and is
never a row here. This type therefore only ever describes user-created rows.
"""
from __future__ import annotations

from dataclasses import dataclass

MANUAL = "manual"
SMART = "smart"
KINDS = (MANUAL, SMART)


@dataclass(frozen=True, slots=True)
class Collection:
    collection_id: str
    name: str
    kind: str
    rules_json: str | None
    sort_order: int
    created_at: str
    updated_at: str
