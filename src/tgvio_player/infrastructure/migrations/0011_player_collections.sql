PRAGMA foreign_keys=ON;

-- User-owned collections. ``favorites`` keeps its own table and its own backup
-- chain, so it is never mirrored here: the "favorites" collection the API
-- exposes is a builtin projection over that table.
CREATE TABLE collections (
    collection_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK(kind IN ('manual','smart')),
    rules_json    TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The foreign keys are the cleanup contract: a media row that is really deleted
-- takes its memberships with it, so a collection can never keep a dangling
-- member. A video that is merely retired (media.active=0) is filtered on read.
CREATE TABLE collection_items (
    collection_id TEXT NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
    media_id      TEXT NOT NULL REFERENCES media(media_id) ON DELETE CASCADE,
    position      INTEGER NOT NULL DEFAULT 0,
    added_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(collection_id, media_id)
);

CREATE INDEX idx_collection_items_media ON collection_items(media_id);
CREATE INDEX idx_collections_order ON collections(sort_order, created_at);
