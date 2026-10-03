"""Collections and their members, as a mixin beside the other repositories.

The aggregate repository composes behaviour through mixins and this file keeps
``sqlite.py`` inside its 1000-line budget. Two rules shape everything here:

* Membership lifetime follows the media row. A real ``DELETE FROM media``
  cascades out of ``collection_items`` (the schema owns that), while a video
  that is merely retired comes back when it is re-supplied - so the member list
  and the counts filter on ``media.active`` and never delete membership.
* Stored rules are read with the same tolerance as the smart-collection
  evaluator: this layer refuses to *store* something it cannot read back as an
  object, and anything it cannot read later is the empty selection, never the
  whole library.
"""
from __future__ import annotations

import json
import uuid

from tgvio_player.domain.collection import Collection, KINDS
from tgvio_player.domain.library_filters import LibraryFilters
from tgvio_player.infrastructure.video_query import filter_clauses

MAX_NAME_LENGTH = 60
MAX_RULES_LENGTH = 4096

_COLUMNS = "collection_id, name, kind, rules_json, sort_order, created_at, updated_at"


def clean_collection_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("collection name must be text")
    cleaned = name.strip()
    if not 1 <= len(cleaned) <= MAX_NAME_LENGTH:
        raise ValueError(f"collection name must be 1..{MAX_NAME_LENGTH} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise ValueError("collection name must not contain control characters")
    return cleaned


def clean_rules_json(rules_json: str | None) -> str | None:
    if rules_json is None:
        return None
    if not isinstance(rules_json, str) or not 1 <= len(rules_json) <= MAX_RULES_LENGTH:
        raise ValueError("collection rules must be a JSON object")
    try:
        payload = json.loads(rules_json)
    except ValueError:
        raise ValueError("collection rules must be a JSON object") from None
    if not isinstance(payload, dict):
        raise ValueError("collection rules must be a JSON object")
    return rules_json


def clean_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError("collection kind must be manual or smart")
    return kind


class PlayerCollectionRepositoryMixin:
    @staticmethod
    def _collection(row) -> Collection:
        return Collection(
            collection_id=str(row["collection_id"]),
            name=str(row["name"]),
            kind=str(row["kind"]),
            rules_json=None if row["rules_json"] is None else str(row["rules_json"]),
            sort_order=int(row["sort_order"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    # The API contract names these methods without a prefix; the repository has no
    # other ``list``/``create``/``delete``, so the plain names stay unambiguous.
    async def list(self) -> tuple[Collection, ...]:
        # Insertion order is the user's order. ``created_at`` has one-second
        # resolution, so the rowid breaks the tie that a random primary key would
        # otherwise resolve by reshuffling the list.
        rows = self._require().execute(
            f"SELECT {_COLUMNS} FROM collections "
            "ORDER BY sort_order, created_at, rowid"
        ).fetchall()
        return tuple(self._collection(row) for row in rows)

    async def get(self, collection_id: str) -> Collection | None:
        row = self._require().execute(
            f"SELECT {_COLUMNS} FROM collections WHERE collection_id=?", (collection_id,)
        ).fetchone()
        return None if row is None else self._collection(row)

    async def create(self, name: str, kind: str, rules_json: str | None) -> Collection:
        cleaned_name = clean_collection_name(name)
        cleaned_kind = clean_kind(kind)
        cleaned_rules = clean_rules_json(rules_json)
        collection_id = uuid.uuid4().hex
        async with self._write_transaction() as conn:
            conn.execute(
                "INSERT INTO collections(collection_id, name, kind, rules_json) VALUES(?,?,?,?)",
                (collection_id, cleaned_name, cleaned_kind, cleaned_rules),
            )
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM collections WHERE collection_id=?", (collection_id,)
            ).fetchone()
        return self._collection(row)

    async def rename(self, collection_id: str, name: str) -> bool:
        cleaned_name = clean_collection_name(name)
        async with self._write_transaction() as conn:
            cursor = conn.execute(
                "UPDATE collections SET name=?, updated_at=CURRENT_TIMESTAMP WHERE collection_id=?",
                (cleaned_name, collection_id),
            )
            return cursor.rowcount > 0

    async def set_rules(self, collection_id: str, rules_json: str | None) -> bool:
        cleaned_rules = clean_rules_json(rules_json)
        async with self._write_transaction() as conn:
            cursor = conn.execute(
                "UPDATE collections SET rules_json=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE collection_id=?",
                (cleaned_rules, collection_id),
            )
            return cursor.rowcount > 0

    async def set_sort_order(self, collection_id: str, sort_order: int) -> bool:
        """The viewer's own order. Any integer is a legal key: a negative one simply
        sorts before the default, which is how a row moves to the front."""
        if isinstance(sort_order, bool) or not isinstance(sort_order, int):
            raise ValueError("collection order must be an integer")
        async with self._write_transaction() as conn:
            cursor = conn.execute(
                "UPDATE collections SET sort_order=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE collection_id=?",
                (int(sort_order), collection_id),
            )
            return cursor.rowcount > 0

    async def delete(self, collection_id: str) -> bool:
        async with self._write_transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM collections WHERE collection_id=?", (collection_id,)
            )
            return cursor.rowcount > 0

    async def add_item(self, collection_id: str, media_id: str) -> bool:
        """True only when this call created the membership.

        An unknown collection or media is a no-op rather than an integrity error:
        every path into here is an idempotent retry from a client that may have
        been looking at a stale list.
        """
        async with self._write_transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM collections WHERE collection_id=?", (collection_id,)
            ).fetchone() is None:
                return False
            if conn.execute(
                "SELECT 1 FROM media WHERE media_id=?", (media_id,)
            ).fetchone() is None:
                return False
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM collection_items "
                "WHERE collection_id=?",
                (collection_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO collection_items(collection_id, media_id, position) VALUES(?,?,?) "
                "ON CONFLICT(collection_id, media_id) DO NOTHING",
                (collection_id, media_id, int(position)),
            )
            return cursor.rowcount > 0

    async def remove_item(self, collection_id: str, media_id: str) -> bool:
        async with self._write_transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM collection_items WHERE collection_id=? AND media_id=?",
                (collection_id, media_id),
            )
            return cursor.rowcount > 0

    async def restore_item(self, collection_id: str, media_id: str) -> bool:
        """Add a membership from a restored backup, placeholder and all.

        A backup can name a video this server has not catalogued yet - a reinstall
        restores before the archive sync finishes. The video gets the same inactive
        placeholder row a pending favourite gets, so the membership is real and the
        video joins the collection the moment the catalog activates it. Unlike
        ``add_item``, this is only reachable from a verified backup payload, which is
        why it may create the row that ``add_item`` refuses to invent.
        """
        async with self._write_transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM collections WHERE collection_id=?", (collection_id,)
            ).fetchone() is None:
                return False
            conn.execute(
                """
                INSERT INTO media(media_id, kind, size_bytes, active, first_seen_at, last_seen_at)
                VALUES(?, 'video', 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(media_id) DO NOTHING
                """,
                (media_id,),
            )
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM collection_items "
                "WHERE collection_id=?",
                (collection_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO collection_items(collection_id, media_id, position) VALUES(?,?,?) "
                "ON CONFLICT(collection_id, media_id) DO NOTHING",
                (collection_id, media_id, int(position)),
            )
            return cursor.rowcount > 0

    async def items(
        self,
        collection_id: str,
        limit: int,
        offset: int = 0,
        *,
        filters: LibraryFilters | None = None,
        favorite_scope: str = "session",
        favorite_token_digest: str | None = None,
    ) -> tuple[str, ...]:
        """Active members only, in the order they were added.

        Insertion order, not a rating: the user arranged nothing else, and the
        position column is what keeps a re-added member at the end. A filter set
        narrows the members through the *same* clause builder the wall uses, so
        "short films with a cover" cannot mean two things in one product.
        """
        clauses, params = filter_clauses(
            min_seconds=None if filters is None else filters.min_seconds,
            max_seconds=None if filters is None else filters.max_seconds,
            date_from=None if filters is None else filters.date_from,
            date_to=None if filters is None else filters.date_to,
            min_bytes=None if filters is None else filters.min_bytes,
            max_bytes=None if filters is None else filters.max_bytes,
            has_cover=None if filters is None else filters.has_cover,
            favorite=None if filters is None else filters.favorite,
            favorite_scope=favorite_scope,
            favorite_token_digest=favorite_token_digest,
            resumable=None if filters is None else filters.resumable,
            unwatched=None if filters is None else filters.unwatched,
        )
        rows = self._require().execute(
            "SELECT item.media_id FROM collection_items item "
            "WHERE item.collection_id=? AND item.media_id IN "
            f"(SELECT media_id FROM media WHERE {' AND '.join(clauses)}) "
            "ORDER BY item.position, item.added_at, item.media_id LIMIT ? OFFSET ?",
            (collection_id, *params, max(0, int(limit)), max(0, int(offset))),
        ).fetchall()
        return tuple(str(row["media_id"]) for row in rows)

    async def counts(self) -> dict[str, int]:
        rows = self._require().execute(
            """
            SELECT collection.collection_id AS collection_id,
                   COUNT(media.media_id) AS member_count
            FROM collections collection
            LEFT JOIN collection_items item
                ON item.collection_id=collection.collection_id
            LEFT JOIN media
                ON media.media_id=item.media_id AND media.active=1 AND media.kind='video'
            GROUP BY collection.collection_id
            """
        ).fetchall()
        return {str(row["collection_id"]): int(row["member_count"]) for row in rows}
