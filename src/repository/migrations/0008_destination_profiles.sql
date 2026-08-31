CREATE TABLE destination_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  destination_peer TEXT NOT NULL,
  discussion_group_peer TEXT,
  channel_at TEXT NOT NULL DEFAULT '',
  group_at TEXT NOT NULL DEFAULT '',
  cover_mode INTEGER NOT NULL DEFAULT 0,
  forward_caption INTEGER NOT NULL DEFAULT 0,
  default_spoiler_mode TEXT NOT NULL DEFAULT 'ask',
  backup_policy TEXT NOT NULL DEFAULT 'best_effort',
  footer_template TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0,
  read_only INTEGER NOT NULL DEFAULT 0,
  source_kind TEXT NOT NULL DEFAULT 'user',
  verified_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX idx_destination_profiles_one_default
ON destination_profiles(is_default)
WHERE is_default=1;

ALTER TABLE jobs ADD COLUMN destination_profile_id INTEGER REFERENCES destination_profiles(id);
ALTER TABLE jobs ADD COLUMN destination_profile_snapshot_json TEXT;

CREATE INDEX idx_jobs_destination_profile ON jobs(destination_profile_id, state, id);
