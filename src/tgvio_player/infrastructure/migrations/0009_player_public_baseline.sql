PRAGMA foreign_keys=ON;

CREATE TABLE catalog_packages (
    package_id TEXT PRIMARY KEY,
    remote_path TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL,
    manifest_etag TEXT,
    complete_etag TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE media (
    media_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    width INTEGER,
    height INTEGER,
    duration_seconds REAL,
    container TEXT,
    codec TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE media_locations (
    package_id TEXT NOT NULL REFERENCES catalog_packages(package_id) ON DELETE CASCADE,
    media_id TEXT NOT NULL REFERENCES media(media_id) ON DELETE CASCADE,
    remote_relpath TEXT NOT NULL,
    remote_etag TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(package_id, remote_relpath)
);

CREATE INDEX idx_media_active_kind
    ON media(active, kind);

CREATE INDEX idx_media_locations_media_active
    ON media_locations(media_id, active);

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

CREATE TABLE player_long_video_progress (
    media_id TEXT PRIMARY KEY REFERENCES media(media_id) ON DELETE CASCADE,
    position_seconds REAL NOT NULL CHECK(position_seconds >= 0),
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_player_long_video_progress_updated
    ON player_long_video_progress(updated_at DESC);

CREATE TABLE player_deleted_locations (
    package_id TEXT NOT NULL,
    remote_relpath TEXT NOT NULL,
    media_id TEXT NOT NULL,
    deleted_at INTEGER NOT NULL,
    PRIMARY KEY(package_id, remote_relpath)
);

CREATE INDEX idx_player_deleted_locations_media
    ON player_deleted_locations(media_id);

CREATE TABLE player_global_favorites (
    media_id TEXT PRIMARY KEY REFERENCES media(media_id),
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_player_global_favorites_page
    ON player_global_favorites(created_at DESC, media_id ASC);

CREATE TABLE player_storage_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    endpoint_url TEXT NOT NULL,
    player_root TEXT NOT NULL,
    favorites_dir TEXT NOT NULL,
    username_ciphertext BLOB,
    password_ciphertext BLOB,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0)
);

CREATE TABLE favorite_sync (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id TEXT NOT NULL REFERENCES media(media_id),
    operation TEXT NOT NULL CHECK(operation IN ('upload','delete')),
    status TEXT NOT NULL CHECK(status IN ('pending','running','retry','synced','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    error_code TEXT,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    claimed_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, intent_persisted INTEGER NOT NULL DEFAULT 0 CHECK(intent_persisted IN (0,1)),
    CHECK(status != 'synced' OR error_code IS NULL)
);

CREATE INDEX idx_favorite_sync_claim
    ON favorite_sync(status, next_attempt_at, job_id);

CREATE INDEX idx_favorite_sync_media
    ON favorite_sync(media_id, operation, status);

CREATE TABLE favorite_locations (
    media_id TEXT PRIMARY KEY REFERENCES media(media_id),
    relpath TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    mime_type TEXT NOT NULL,
    verified_at INTEGER NOT NULL
);

CREATE TABLE media_variants (
    variant_media_id TEXT PRIMARY KEY REFERENCES media(media_id) ON DELETE CASCADE,
    parent_media_id TEXT NOT NULL REFERENCES media(media_id) ON DELETE CASCADE,
    height INTEGER NOT NULL CHECK(height > 0),
    width INTEGER,
    bitrate_bps INTEGER,
    label TEXT,
    rank INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_media_variants_parent
    ON media_variants(parent_media_id);

INSERT INTO player_storage_settings(
    singleton, endpoint_url, player_root, favorites_dir, revision
) VALUES(1, 'https://webdav.example.invalid/dav', 'Player', 'Favorites', 0);
