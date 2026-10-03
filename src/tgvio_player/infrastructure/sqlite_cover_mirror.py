"""Cover mirror candidates as their own mixin, beside the other repository mixins.

The aggregate repository composes behaviour through mixins, and this keeps ``sqlite.py``
inside its 1000-line budget. This file only answers one question: which covers exist, where
do their bytes live, and how big are they.
"""
from __future__ import annotations


class PlayerCoverMirrorRepositoryMixin:
    async def mirror_candidates(self) -> tuple[tuple[str, str, str, int], ...]:
        """Every active cover that could be mirrored, as (media_id, remote_path, relpath, size).

        The whole active set comes back in one query rather than a page of it: the warm loop
        needs all of it to decide what to keep, and a thousand rows is nothing next to the
        single cover read it saves. Retired rows and non-video media never appear here, so a
        retired cover can neither be warmed nor keep a file alive in the mirror.
        """
        rows = self._require().execute(
            """
            SELECT mc.media_id, cp.remote_path, mc.remote_relpath, mc.size_bytes
            FROM media_covers mc
            JOIN catalog_packages cp ON cp.package_id=mc.package_id
            JOIN media ON media.media_id=mc.media_id
            WHERE mc.active=1 AND cp.active=1 AND media.active=1 AND media.kind='video'
              AND mc.size_bytes > 0
            ORDER BY mc.media_id
            """,
        ).fetchall()
        return tuple(
            (str(row[0]), str(row[1]), str(row[2]), int(row[3])) for row in rows
        )
