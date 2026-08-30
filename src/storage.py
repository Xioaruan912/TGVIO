"""Small persistent JSON stores used by the bot.

Keeping file IO here makes the pipeline independent from the on-disk format and
gives future migrations one place to live.  The bot treats these stores as a
best-effort cache: a corrupt or missing file falls back to the supplied default.
"""

import json
import os
from copy import deepcopy
from typing import Any


class JsonStore:
    """A synchronous, best-effort JSON file store for small state documents."""

    def __init__(self, path: str, default: Any):
        self.path = path
        self.default = default

    def load(self) -> Any:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return deepcopy(self.default)

    def save(self, value: Any) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            # Persistence must not stop media processing.
            pass
