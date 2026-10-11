PRAGMA foreign_keys=ON;

-- Pairs of videos the viewer said are not duplicates, so the duplicate review
-- never offers them together again. One row per pair, the smaller id first.
CREATE TABLE player_duplicate_dismissals (
    media_a TEXT NOT NULL REFERENCES media(media_id) ON DELETE CASCADE,
    media_b TEXT NOT NULL REFERENCES media(media_id) ON DELETE CASCADE,
    dismissed_at INTEGER NOT NULL,
    PRIMARY KEY(media_a, media_b),
    CHECK(media_a < media_b)
);
