"""Cover similarity as its own mixin, beside the other repository mixins.

The aggregate repository composes behaviour through mixins, and this keeps
``sqlite.py`` inside its 1000-line budget. The distance itself is a pure function in
``cover_similarity.py``; this file only decides which rows are worth comparing.
"""
from __future__ import annotations

from tgvio_player.infrastructure.cover_similarity import is_fingerprint, nearest_fingerprints


class PlayerSimilarRepositoryMixin:
    async def similar_cover_ids(
        self, phash: str, *, media_id: str, threshold: int, limit: int, scan_limit: int
    ) -> tuple[tuple[tuple[str, int], ...], bool]:
        """Covers whose fingerprint is near this one, from a bounded scan.

        The scan is capped because a personal library can grow past any single request's
        budget; when the cap is hit the caller is told, rather than being handed a
        silently partial answer. A row whose stored fingerprint is unreadable is skipped,
        never compared.
        """
        if not is_fingerprint(phash):
            return (), False
        rows = self._require().execute(
            """
            SELECT mc.media_id, mc.phash
            FROM media_covers mc
            JOIN media ON media.media_id=mc.media_id
            WHERE mc.active=1 AND mc.phash IS NOT NULL AND media.active=1
              AND media.kind='video' AND mc.media_id != ?
            ORDER BY mc.media_id
            LIMIT ?
            """,
            (media_id, max(1, int(scan_limit)) + 1),
        ).fetchall()
        truncated = len(rows) > max(1, int(scan_limit))
        scanned = [(str(row["media_id"]), row["phash"])
                   for row in rows[: max(1, int(scan_limit))]]
        return nearest_fingerprints(
            scanned, phash, threshold=threshold, limit=limit
        ), truncated

