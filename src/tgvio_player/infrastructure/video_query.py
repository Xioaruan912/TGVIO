"""The wall's listing query, built as data so it can be read and tested without a
database.

Every filter is a bound parameter and every one of them is optional; a filter that is
absent is not a constraint at all, never "match nothing". The orders are pure
functions of the row, which is what makes LIMIT/OFFSET paging repeatable.
"""
from __future__ import annotations

from typing import Any

from tgvio_player.domain.library_filters import LibraryFilters, duration_bounds

_BASE_CLAUSES = (
    "active=1",
    "kind='video'",
    "media_id NOT IN (SELECT variant_media_id FROM media_variants)",
)


def shuffle_key_sql(seed: int) -> str:
    """A per-row key that depends only on the id and the seed.

    Paging is only repeatable if the order is a pure function of the row, so this
    cannot be a random number drawn per query. Four characters of the id are folded
    with fixed multipliers, which spreads neighbours apart well enough for a personal
    library and stays inside int64.
    """
    parts = [
        f"instr('0123456789abcdef', substr(media_id,{position},1)) * {multiplier}"
        for position, multiplier in (
            (1, 2654435761),
            (2, 40503),
            (3, 2246822519),
            (4, 3266489917),
        )
    ]
    return f"(({' + '.join(parts)} + {int(seed)}) & 2147483647)"


def _cover_clause() -> str:
    return "EXISTS (SELECT 1 FROM media_covers c WHERE c.media_id=media.media_id AND c.active=1)"


def _favorite_clause(scope: str) -> str:
    if scope == "global":
        return (
            "EXISTS (SELECT 1 FROM player_global_favorites g "
            "WHERE g.media_id=media.media_id)"
        )
    return (
        "EXISTS (SELECT 1 FROM favorites f WHERE f.media_id=media.media_id "
        "AND f.token_digest=?)"
    )


def _started_clause() -> str:
    return (
        "EXISTS (SELECT 1 FROM player_long_video_progress p "
        "WHERE p.media_id=media.media_id AND p.position_seconds > 0)"
    )


def _watched_clause() -> str:
    return (
        "EXISTS (SELECT 1 FROM player_long_video_progress p "
        "WHERE p.media_id=media.media_id)"
    )


def _order_sql(order: str, sort: str | None, seed: int | None) -> str:
    if sort == "newest":
        return "first_seen_at DESC, media_id ASC"
    if sort == "longest":
        return "duration_seconds DESC, media_id ASC"
    if sort == "largest":
        return "size_bytes DESC, media_id ASC"
    if sort == "resume":
        # Rows never started sort last rather than being excluded: this is an order,
        # not a filter, and the wall must not lose videos by using it.
        last_played = (
            "(SELECT p.updated_at FROM player_long_video_progress p "
            "WHERE p.media_id=media.media_id)"
        )
        return f"({last_played} IS NULL), {last_played} DESC, media_id ASC"
    if sort == "random":
        return f"{shuffle_key_sql(int(seed or 0))}, media_id ASC"
    return {
        "duration_desc": "duration_seconds DESC, media_id",
        "duration_asc": "duration_seconds ASC, media_id",
    }.get(order, "media_id")


def listing_query_kwargs(
    filters: LibraryFilters,
    *,
    category: str,
    large_video_seconds: float,
    sort: str | None,
    favorite_scope: str,
    favorite_token_digest: str | None = None,
    media_id_prefix: str | None = None,
) -> dict[str, Any]:
    """One filter set, mapped onto this query's parameters.

    The wall and a smart collection are the same conditions evaluated by the same
    query, so both come through here rather than each assembling the arguments - a
    second assembly would drift the moment one of them learns a new filter.
    """
    min_seconds, max_seconds = duration_bounds(
        filters, category=category, large_video_seconds=large_video_seconds
    )
    return {
        "min_seconds": min_seconds,
        "max_seconds": max_seconds,
        "media_id_prefix": media_id_prefix,
        "order": "duration_desc" if category == "long" else "media_id",
        "date_from": filters.date_from,
        "date_to": filters.date_to,
        "min_bytes": filters.min_bytes,
        "max_bytes": filters.max_bytes,
        "has_cover": filters.has_cover,
        "favorite": filters.favorite,
        "favorite_scope": favorite_scope,
        "favorite_token_digest": favorite_token_digest,
        "resumable": filters.resumable,
        "unwatched": filters.unwatched,
        "sort": sort,
        "seed": filters.seed,
    }


def build_video_query(
    *,
    min_seconds: float | None = None,
    max_seconds: float | None = None,
    media_id_prefix: str | None = None,
    order: str = "media_id",
    limit: int = 1000,
    offset: int = 0,
    date_from: int | None = None,
    date_to: int | None = None,
    min_bytes: int | None = None,
    max_bytes: int | None = None,
    has_cover: bool | None = None,
    favorite: bool | None = None,
    favorite_scope: str = "session",
    favorite_token_digest: str | None = None,
    resumable: bool | None = None,
    unwatched: bool | None = None,
    sort: str | None = None,
    seed: int | None = None,
) -> tuple[str, list[Any]]:
    """The statement and its parameters for one page of the wall."""
    clauses = list(_BASE_CLAUSES)
    params: list[Any] = []
    if min_seconds is not None:
        clauses.append("duration_seconds > ?")
        params.append(float(min_seconds))
    if max_seconds is not None:
        clauses.append("duration_seconds <= ?")
        params.append(float(max_seconds))
    if media_id_prefix is not None:
        clauses.append("media_id LIKE ?")
        params.append(f"{media_id_prefix}%")
    # The timestamp is stored as text, so without the cast SQLite compares a text
    # value against an integer, which is always true, and every row matches.
    if date_from is not None:
        clauses.append("CAST(strftime('%s', first_seen_at) AS INTEGER) >= ?")
        params.append(int(date_from))
    if date_to is not None:
        clauses.append("CAST(strftime('%s', first_seen_at) AS INTEGER) <= ?")
        params.append(int(date_to))
    if min_bytes is not None:
        clauses.append("size_bytes >= ?")
        params.append(int(min_bytes))
    if max_bytes is not None:
        clauses.append("size_bytes <= ?")
        params.append(int(max_bytes))
    if has_cover is not None:
        covered = _cover_clause()
        clauses.append(covered if has_cover else f"NOT {covered}")
    if favorite is not None:
        # The same authority the favourites page reads, so the filter and the page
        # cannot disagree about what "my favourites" means in this runtime.
        favourited = _favorite_clause(favorite_scope)
        if favorite_scope != "global":
            params.append(favorite_token_digest or "")
        clauses.append(favourited if favorite else f"NOT {favourited}")
    if resumable is not None:
        started = _started_clause()
        clauses.append(started if resumable else f"NOT {started}")
    if unwatched is not None:
        watched = _watched_clause()
        clauses.append(f"NOT {watched}" if unwatched else watched)
    params.extend([max(1, int(limit)), max(0, int(offset))])
    return (
        f"SELECT media_id FROM media WHERE {' AND '.join(clauses)} "
        f"ORDER BY {_order_sql(order, sort, seed)} LIMIT ? OFFSET ?",
        params,
    )
