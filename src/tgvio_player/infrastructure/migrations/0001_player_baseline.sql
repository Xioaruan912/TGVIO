PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS catalog_packages (
    package_id TEXT PRIMARY KEY,
    remote_path TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL,
    manifest_etag TEXT,
    complete_etag TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS media (
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

CREATE TABLE IF NOT EXISTS media_locations (
    package_id TEXT NOT NULL REFERENCES catalog_packages(package_id) ON DELETE CASCADE,
    media_id TEXT NOT NULL REFERENCES media(media_id) ON DELETE CASCADE,
    remote_relpath TEXT NOT NULL,
    remote_etag TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(package_id, remote_relpath)
);

CREATE INDEX IF NOT EXISTS idx_media_active_kind
    ON media(active, kind);

CREATE INDEX IF NOT EXISTS idx_media_locations_media_active
    ON media_locations(media_id, active);
