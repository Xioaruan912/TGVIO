PRAGMA foreign_keys=ON;

-- Where each member of a manual collection is copied on the Player storage, one
-- folder per collection named after it. The table records what is really on the
-- storage, not what should be: a background worker compares it with the
-- collection memberships and copies, moves or removes until both agree. A row
-- outlives its membership or collection on purpose (no foreign keys), because
-- its copy still has to be removed from the storage after either is gone.
--   relpath:     path under the Player root once copied; NULL before the first copy.
--   attempts / next_attempt_at / last_error: bounded retry with back-off.
CREATE TABLE collection_backups (
    collection_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    relpath TEXT,
    size_bytes INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY(collection_id, media_id)
);

CREATE INDEX idx_collection_backups_due ON collection_backups(next_attempt_at);
