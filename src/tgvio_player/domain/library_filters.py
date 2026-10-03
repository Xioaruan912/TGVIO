"""What the wall is showing, as one value object both sides of the wire agree on.

A filter set arrives from a query string or comes back out of a stored smart
collection, so it is parsed twice with two different tolerances: a request that
names a filter this build does not know is a client error, while a stored rule blob
that this build cannot read is an empty selection. The second case must never widen
into "the whole library" - that would turn a corrupt row into a privacy surprise.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

SORT_ORDERS = ("newest", "longest", "largest", "random", "resume")
CATEGORIES = ("short", "long")
MAX_TEXT = 64


class InvalidFilters(ValueError):
    """A filter set that a client sent and this build will not accept."""


def _as_bool(value: Any, label: str) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise InvalidFilters(f"invalid {label}")


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise InvalidFilters(f"invalid {label}")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidFilters(f"invalid {label}") from None
    if number < 0:
        raise InvalidFilters(f"invalid {label}")
    return number


def _as_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise InvalidFilters(f"invalid {label}")
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidFilters(f"invalid {label}") from None
    if number < 0:
        raise InvalidFilters(f"invalid {label}")
    return number


def _as_categories(value: Any) -> frozenset[str]:
    if value is None or value == "":
        return frozenset()
    if isinstance(value, str):
        items = [piece.strip() for piece in value.split(",")]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(piece).strip() for piece in value]
    else:
        raise InvalidFilters("invalid categories")
    wanted = frozenset(piece for piece in items if piece)
    if not wanted or not wanted <= set(CATEGORIES):
        raise InvalidFilters("invalid categories")
    return wanted


@dataclass(frozen=True)
class LibraryFilters:
    """An empty field means "do not constrain on this", never "match nothing"."""

    categories: frozenset[str] = frozenset()
    date_from: int | None = None
    date_to: int | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None
    min_bytes: int | None = None
    max_bytes: int | None = None
    has_cover: bool | None = None
    favorite: bool | None = None
    resumable: bool | None = None
    unwatched: bool | None = None
    sort: str = "newest"
    seed: int | None = None

    @classmethod
    def empty(cls) -> "LibraryFilters":
        return cls()

    def to_json(self) -> str:
        """The stored form of a smart collection, readable by `parse_rules`."""
        payload = {
            name: (sorted(value) if isinstance(value, frozenset) else value)
            for name, value in self.__dict__.items()
            if value is not None and value != frozenset() and value != "newest"
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def parse_filters(query: Mapping[str, str]) -> LibraryFilters:
    """Strict: this is what a client asked for."""
    known = {field.name for field in fields(LibraryFilters)}
    filters = LibraryFilters.empty()
    updates: dict[str, Any] = {}
    for key, raw in query.items():
        if key not in known:
            raise InvalidFilters(f"unknown filter: {key}")
        if raw == "":
            continue
        if key == "categories":
            updates["categories"] = _as_categories(raw)
        elif key == "sort":
            if raw not in SORT_ORDERS:
                raise InvalidFilters("invalid sort")
            updates["sort"] = raw
        elif key in {"has_cover", "favorite", "resumable", "unwatched"}:
            updates[key] = _as_bool(raw, key)
        elif key in {"date_from", "date_to", "min_bytes", "max_bytes", "seed"}:
            updates[key] = _as_int(raw, key)
        elif key in {"min_seconds", "max_seconds"}:
            updates[key] = _as_float(raw, key)
    filters = replace(filters, **updates)
    if filters.sort == "random" and filters.seed is None:
        raise InvalidFilters("random order needs a seed")
    if filters.sort != "random" and filters.seed is not None:
        raise InvalidFilters("a seed only means something for random order")
    for low, high in (("date_from", "date_to"), ("min_seconds", "max_seconds"), ("min_bytes", "max_bytes")):
        lower, upper = getattr(filters, low), getattr(filters, high)
        if lower is not None and upper is not None and lower > upper:
            raise InvalidFilters(f"{low} is above {high}")
    return filters


def parse_rules(value: Any) -> LibraryFilters:
    """Lenient: this is what a stored smart collection remembers.

    Anything unreadable - absent, empty, not JSON, not an object, or carrying a key
    this build does not know - is the empty selection. Widening it to the whole
    library would make a single corrupt row show everything.
    """
    if value is None or value == "":
        return LibraryFilters.empty()
    if isinstance(value, str):
        if len(value) > 4096:
            return LibraryFilters.empty()
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return LibraryFilters.empty()
    if not isinstance(value, dict):
        return LibraryFilters.empty()
    known = {field.name for field in fields(LibraryFilters)}
    if any(key not in known for key in value):
        return LibraryFilters.empty()
    # The stored form is JSON, so its booleans are real booleans while the strict
    # parser speaks the query-string vocabulary. Normalise here instead of asking
    # the strict parser to accept a second spelling of every value.
    normalized: dict[str, str] = {}
    for key, raw in value.items():
        if isinstance(raw, bool):
            normalized[key] = "true" if raw else "false"
        elif isinstance(raw, (list, tuple, set, frozenset)):
            normalized[key] = ",".join(str(piece) for piece in raw)
        else:
            normalized[key] = str(raw)
    try:
        return parse_filters(normalized)
    except InvalidFilters:
        return LibraryFilters.empty()
