from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class OperationToken:
    token: str
    owner_id: int
    action: str
    resource_type: str
    resource_id: str
    expected_revision: int
    payload_hash: str
    payload: dict[str, Any] = field(default_factory=dict)
    expires_at: int = 0
    consumed_at: int | None = None
    created_at: str | None = None

