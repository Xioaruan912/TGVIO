from __future__ import annotations


class PlayerMediaWorkRepositoryMixin:
    """Queues for the background media work: duration probes and 480p copies."""

    async def list_unprobed_video_ids(self, *, limit: int) -> list[str]:
        """Active videos still without a duration: plain files the probe has not read yet."""
        rows = self._require().execute(
            "SELECT media_id FROM media WHERE active=1 AND kind='video' AND duration_seconds IS NULL "
            "ORDER BY first_seen_at DESC, media_id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [str(row["media_id"]) for row in rows]

    async def list_rendition_candidates(self, *, limit: int) -> list[dict[str, object]]:
        """Videos taller than 480p with a known duration and no 480p copy yet."""
        rows = self._require().execute(
            """
            SELECT m.media_id, m.size_bytes, m.duration_seconds, m.height,
                   cp.remote_path, ml.remote_relpath
            FROM media m
            JOIN media_locations ml ON ml.media_id=m.media_id AND ml.active=1
            JOIN catalog_packages cp ON cp.package_id=ml.package_id AND cp.active=1
            WHERE m.active=1 AND m.kind='video' AND m.height > 480 AND m.duration_seconds > 0
              AND m.media_id NOT IN (SELECT variant_media_id FROM media_variants)
              AND NOT EXISTS (SELECT 1 FROM media_variants mv WHERE mv.parent_media_id=m.media_id AND mv.height=480)
            GROUP BY m.media_id
            ORDER BY m.first_seen_at DESC, m.media_id
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]

    async def save_media_probe(
        self, media_id: str, duration_seconds: float | None, width: int | None, height: int | None,
    ) -> None:
        async with self._write_transaction() as conn:
            conn.execute(
                "UPDATE media SET duration_seconds=COALESCE(?, duration_seconds), "
                "width=COALESCE(?, width), height=COALESCE(?, height) WHERE media_id=?",
                (duration_seconds, width, height, media_id),
            )

    async def list_cover_candidates(self, *, limit: int) -> list[dict[str, object]]:
        """Videos with a known duration and no active cover."""
        rows = self._require().execute(
            """
            SELECT m.media_id, m.size_bytes, m.duration_seconds, cp.remote_path, ml.remote_relpath
            FROM media m
            JOIN media_locations ml ON ml.media_id=m.media_id AND ml.active=1
            JOIN catalog_packages cp ON cp.package_id=ml.package_id AND cp.active=1
            WHERE m.active=1 AND m.kind='video' AND m.duration_seconds > 0
              AND m.media_id NOT IN (SELECT variant_media_id FROM media_variants)
              AND NOT EXISTS (SELECT 1 FROM media_covers c WHERE c.media_id=m.media_id AND c.active=1)
            GROUP BY m.media_id
            ORDER BY m.first_seen_at DESC, m.media_id
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]
