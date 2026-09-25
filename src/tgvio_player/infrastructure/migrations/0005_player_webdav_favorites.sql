PRAGMA foreign_keys=ON;

CREATE TABLE player_global_favorites (
    media_id TEXT PRIMARY KEY REFERENCES media(media_id),
    created_at INTEGER NOT NULL
);

INSERT INTO player_global_favorites(media_id, created_at)
SELECT media_id, MIN(created_at)
FROM favorites
GROUP BY media_id;

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

INSERT INTO player_storage_settings(
    singleton, endpoint_url, player_root, favorites_dir, revision
) VALUES(1, 'https://file.722225.xyz', '115/Pron/99_TGPLAYER', '99_收藏', 0);

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
    updated_at INTEGER NOT NULL,
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
