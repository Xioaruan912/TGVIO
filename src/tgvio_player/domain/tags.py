"""The viewer's own labels for videos."""
from __future__ import annotations

from dataclasses import dataclass
import unicodedata

MAX_NAME_LENGTH = 20
MAX_TAGS = 200


class InvalidTag(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Tag:
    tag_id: int
    name: str
    count: int = 0


def clean_tag_name(value: object) -> str:
    """One line of 1 to 20 visible characters, inner spaces kept."""
    if not isinstance(value, str):
        raise InvalidTag("a tag name is text")
    name = " ".join(value.split())
    if not name or len(name) > MAX_NAME_LENGTH:
        raise InvalidTag(f"a tag name has 1 to {MAX_NAME_LENGTH} characters")
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise InvalidTag("a tag name has no control characters")
    return name
