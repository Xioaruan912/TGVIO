from __future__ import annotations

from dataclasses import dataclass

# The copies of each stored-twice video, best first: the copy whose package also
# holds the video's cover, then the one holding its renditions, then the oldest
# package (archive paths start with the date folder).
_RANKED_COPIES = """
    SELECT ml.media_id, ml.package_id, cp.remote_path, ml.remote_relpath,
        ROW_NUMBER() OVER (PARTITION BY ml.media_id ORDER BY
            EXISTS (SELECT 1 FROM media_covers c WHERE c.media_id=ml.media_id
                    AND c.package_id=ml.package_id AND c.active=1) DESC,
            EXISTS (SELECT 1 FROM media_variants v JOIN media_locations vl
                    ON vl.media_id=v.variant_media_id AND vl.active=1
                    WHERE v.parent_media_id=ml.media_id AND vl.package_id=ml.package_id) DESC,
            cp.remote_path, ml.remote_relpath) AS rank
    FROM media_locations ml
    JOIN catalog_packages cp ON cp.package_id=ml.package_id AND cp.active=1
    JOIN media m ON m.media_id=ml.media_id AND m.kind='video'
    WHERE ml.active=1
      AND ml.media_id NOT IN (SELECT variant_media_id FROM media_variants)
      AND ml.media_id NOT IN (SELECT media_id FROM player_media_deletions)
"""


@dataclass(frozen=True, slots=True)
class StoredCopy:
    package_id: str
    package_path: str
    relpath: str


@dataclass(frozen=True, slots=True)
class RedundantCopy:
    media_id: str
    keep: StoredCopy
    extra: StoredCopy
    size_bytes: int


class PlayerDuplicateCopyRepositoryMixin:
    """Videos the archive stores more than once (the same file in several packages)."""

    async def redundant_copies(self, *, limit: int) -> list[RedundantCopy]:
        rows = self._require().execute(
            f"""
            WITH ranked AS ({_RANKED_COPIES})
            SELECT extra.media_id, m.size_bytes,
                keep.package_id, keep.remote_path, keep.remote_relpath,
                extra.package_id, extra.remote_path, extra.remote_relpath
            FROM ranked extra
            JOIN ranked keep ON keep.media_id=extra.media_id AND keep.rank=1
            JOIN media m ON m.media_id=extra.media_id
            WHERE extra.rank > 1
            ORDER BY extra.media_id, extra.rank
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [
            RedundantCopy(str(row[0]), StoredCopy(str(row[2]), str(row[3]), str(row[4])),
                          StoredCopy(str(row[5]), str(row[6]), str(row[7])), int(row[1]))
            for row in rows
        ]

    async def is_redundant_copy(self, copy: RedundantCopy) -> bool:
        """Still a spare: both copies active and the video not being deleted."""
        row = self._require().execute(
            """
            SELECT COUNT(*) FROM media_locations ml
            JOIN catalog_packages cp ON cp.package_id=ml.package_id AND cp.active=1
            WHERE ml.media_id=? AND ml.active=1
              AND ((ml.package_id=? AND ml.remote_relpath=?) OR (ml.package_id=? AND ml.remote_relpath=?))
              AND NOT EXISTS (SELECT 1 FROM player_media_deletions d WHERE d.media_id=ml.media_id)
            """,
            (copy.media_id, copy.keep.package_id, copy.keep.relpath,
             copy.extra.package_id, copy.extra.relpath),
        ).fetchone()
        return int(row[0]) == 2
