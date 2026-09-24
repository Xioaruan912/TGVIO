CREATE TABLE IF NOT EXISTS player_deleted_locations (
    package_id TEXT NOT NULL,
    remote_relpath TEXT NOT NULL,
    media_id TEXT NOT NULL,
    deleted_at INTEGER NOT NULL,
    PRIMARY KEY(package_id, remote_relpath)
);

CREATE INDEX IF NOT EXISTS idx_player_deleted_locations_media
    ON player_deleted_locations(media_id);
