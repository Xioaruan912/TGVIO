from __future__ import annotations

from itertools import combinations
import time

from tgvio_player.domain.duplicates import Candidate, pair
from tgvio_player.infrastructure.cover_similarity import is_fingerprint


class PlayerDuplicateReviewRepositoryMixin:
    """What the duplicate review compares, and the pairs it must not offer again."""

    async def duplicate_candidates(self) -> list[Candidate]:
        rows = self._require().execute(
            """
            SELECT media.media_id, MIN(mc.phash) AS phash, media.duration_seconds
            FROM media
            JOIN media_covers mc ON mc.media_id=media.media_id AND mc.active=1
            WHERE media.active=1 AND media.kind='video' AND mc.phash IS NOT NULL
              AND media.duration_seconds IS NOT NULL
              AND media.media_id NOT IN (SELECT variant_media_id FROM media_variants)
            GROUP BY media.media_id
            """
        ).fetchall()
        return [Candidate(str(row[0]), str(row[1]), float(row[2]))
                for row in rows if is_fingerprint(row[1])]

    async def dismissed_duplicate_pairs(self) -> set[tuple[str, str]]:
        rows = self._require().execute(
            "SELECT media_a, media_b FROM player_duplicate_dismissals"
        ).fetchall()
        return {(str(row[0]), str(row[1])) for row in rows}

    async def dismiss_duplicates(self, media_ids: list[str]) -> int:
        """Record every pair of these videos as not duplicates; unknown ids are ignored."""
        now = int(time.time())
        async with self._write_transaction() as conn:
            known = sorted({
                str(row[0]) for row in conn.execute(
                    f"SELECT media_id FROM media WHERE media_id IN ({','.join('?' * len(media_ids))})",
                    media_ids,
                )
            })
            conn.executemany(
                "INSERT OR IGNORE INTO player_duplicate_dismissals(media_a, media_b, dismissed_at) "
                "VALUES(?,?,?)",
                [(*pair(a, b), now) for a, b in combinations(known, 2)],
            )
        return len(known)
