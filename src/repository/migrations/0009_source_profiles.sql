CREATE TABLE source_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  source_peer TEXT NOT NULL,
  source_peer_id INTEGER NOT NULL UNIQUE,
  destination_profile_id INTEGER NOT NULL REFERENCES destination_profiles(id),
  owner_user_id INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  album_gather_seconds REAL NOT NULL DEFAULT 2.0,
  spoiler_policy TEXT NOT NULL DEFAULT 'normal',
  caption_policy TEXT NOT NULL DEFAULT 'preserve',
  backup_policy TEXT NOT NULL DEFAULT 'inherit',
  verified_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE INDEX idx_source_profiles_enabled_peer
ON source_profiles(enabled, source_peer_id);

CREATE TABLE source_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_profile_id INTEGER NOT NULL REFERENCES source_profiles(id),
  source_peer_id INTEGER NOT NULL,
  source_message_id INTEGER NOT NULL,
  grouped_id INTEGER,
  state TEXT NOT NULL DEFAULT 'received',
  job_legacy_seq INTEGER,
  error_code TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(source_peer_id, source_message_id)
);

CREATE INDEX idx_source_events_group_state
ON source_events(source_profile_id, grouped_id, state, id);
