ALTER TABLE job_items ADD COLUMN content_sha256 TEXT;

CREATE INDEX idx_job_items_content_sha256
ON job_items(content_sha256, size_bytes);

CREATE TABLE dedup_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  media_kind TEXT NOT NULL,
  destination_key TEXT NOT NULL,
  source_peer_id INTEGER NOT NULL,
  source_message_id INTEGER NOT NULL,
  media_id INTEGER,
  access_hash INTEGER,
  file_reference BLOB,
  metadata_json TEXT,
  verified_at REAL NOT NULL,
  last_used_at REAL NOT NULL,
  hit_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(sha256, size_bytes, media_kind, destination_key)
);

CREATE INDEX idx_dedup_last_used ON dedup_entries(last_used_at);
