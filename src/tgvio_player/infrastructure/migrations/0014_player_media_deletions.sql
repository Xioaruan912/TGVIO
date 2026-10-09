PRAGMA foreign_keys=ON;

-- A permanent delete the viewer asked for. The row is written, and the media
-- hidden, before the request answers; the archive files are removed afterwards
-- by one background worker that retries until every copy is gone. The row is
-- removed only when the deletion is complete, so the table holds at most one
-- row per media that is still being deleted.
--   undo_until: the deletion may still be cancelled before this time (seconds).
--   started:    1 once the worker began removing files; it can no longer be undone.
--   attempts / next_attempt_at / last_error: bounded retry with back-off.
CREATE TABLE player_media_deletions (
    media_id TEXT PRIMARY KEY,
    requested_at INTEGER NOT NULL,
    undo_until INTEGER NOT NULL,
    started INTEGER NOT NULL DEFAULT 0 CHECK(started IN (0,1)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at INTEGER NOT NULL,
    last_error TEXT
);

CREATE INDEX idx_player_media_deletions_due
    ON player_media_deletions(next_attempt_at);
