PRAGMA foreign_keys=ON;

CREATE TABLE player_sessions (
    token_digest TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE feed_sessions (
    token_digest TEXT PRIMARY KEY REFERENCES player_sessions(token_digest) ON DELETE CASCADE,
    current_cycle INTEGER NOT NULL DEFAULT 0 CHECK(current_cycle >= 0),
    last_active_at INTEGER NOT NULL
);

CREATE TABLE feed_session_items (
    token_digest TEXT NOT NULL REFERENCES player_sessions(token_digest) ON DELETE CASCADE,
    cycle INTEGER NOT NULL CHECK(cycle > 0),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    media_id TEXT NOT NULL REFERENCES media(media_id),
    consumed_at INTEGER,
    PRIMARY KEY(token_digest, cycle, ordinal),
    UNIQUE(token_digest, cycle, media_id)
);

CREATE INDEX idx_feed_session_items_next
    ON feed_session_items(token_digest, cycle, consumed_at, ordinal);

CREATE TABLE feed_recent_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_digest TEXT NOT NULL REFERENCES player_sessions(token_digest) ON DELETE CASCADE,
    media_id TEXT NOT NULL,
    consumed_at INTEGER NOT NULL
);

CREATE INDEX idx_feed_recent_media_lookup
    ON feed_recent_media(token_digest, id DESC);

CREATE TABLE favorites (
    token_digest TEXT NOT NULL REFERENCES player_sessions(token_digest) ON DELETE CASCADE,
    media_id TEXT NOT NULL REFERENCES media(media_id),
    created_at INTEGER NOT NULL,
    PRIMARY KEY(token_digest, media_id)
);
