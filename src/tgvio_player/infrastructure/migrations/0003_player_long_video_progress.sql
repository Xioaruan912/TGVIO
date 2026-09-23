PRAGMA foreign_keys=ON;

-- TGVIO Player is a single-owner private player. Progress is deliberately
-- shared across that owner's authenticated sessions, including iPhone Safari
-- and its Home Screen app, rather than being keyed to a temporary session.
CREATE TABLE player_long_video_progress (
    media_id TEXT PRIMARY KEY REFERENCES media(media_id) ON DELETE CASCADE,
    position_seconds REAL NOT NULL CHECK(position_seconds >= 0),
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_player_long_video_progress_updated
    ON player_long_video_progress(updated_at DESC);
