PRAGMA foreign_keys=ON;

-- Versioned still covers carried by Archive packages. A cover is optional
-- metadata: a missing row only means "no cover", never an unplayable video, so
-- this table is deliberately additive and carries no foreign keys that could
-- fail on either migration lineage.
CREATE TABLE media_covers (
    package_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    remote_relpath TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    mime_type TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(package_id, media_id)
);

CREATE INDEX idx_media_covers_media_active
    ON media_covers(media_id, active);
