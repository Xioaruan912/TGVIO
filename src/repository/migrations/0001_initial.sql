CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at REAL NOT NULL,
  checksum TEXT NOT NULL
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  state TEXT NOT NULL,
  resume_state TEXT,
  download_state TEXT NOT NULL DEFAULT 'pending',
  publish_state TEXT NOT NULL DEFAULT 'pending',
  backup_state TEXT NOT NULL DEFAULT 'disabled',
  backup_policy TEXT NOT NULL DEFAULT 'best_effort',
  spoiler INTEGER NOT NULL DEFAULT 0,
  source_kind TEXT NOT NULL,
  source_chat_id INTEGER,
  source_url TEXT,
  status_chat_id INTEGER,
  status_message_id INTEGER,
  local_dir TEXT,
  bytes_done INTEGER NOT NULL DEFAULT 0,
  bytes_total INTEGER NOT NULL DEFAULT 0,
  current_item INTEGER NOT NULL DEFAULT 0,
  total_items INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at REAL,
  error_code TEXT,
  error_message TEXT,
  publish_result_json TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  accepted_at REAL NOT NULL,
  started_at REAL,
  updated_at REAL NOT NULL,
  finished_at REAL
);

CREATE INDEX idx_jobs_state_order ON jobs(state, id);
CREATE INDEX idx_jobs_user_updated ON jobs(user_id, updated_at DESC);
CREATE INDEX idx_jobs_retry ON jobs(state, next_retry_at);

CREATE TABLE job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  payload_json TEXT,
  created_at REAL NOT NULL
);

CREATE INDEX idx_job_events_job ON job_events(job_id, id);

CREATE TABLE backup_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  remote_dir TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at REAL,
  error_code TEXT,
  error_message TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  finished_at REAL
);

CREATE INDEX idx_backup_attempts_job ON backup_attempts(job_id, id);
CREATE INDEX idx_backup_attempts_retry ON backup_attempts(state, next_retry_at);

CREATE TABLE backup_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES backup_attempts(id) ON DELETE CASCADE,
  job_item_id INTEGER,
  local_path TEXT NOT NULL,
  remote_name TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  state TEXT NOT NULL,
  bytes_done INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  error_message TEXT,
  UNIQUE(attempt_id, remote_name)
);

CREATE INDEX idx_backup_files_attempt ON backup_files(attempt_id, id);

CREATE TABLE settings (
  scope TEXT NOT NULL DEFAULT 'global',
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at REAL NOT NULL,
  PRIMARY KEY(scope, key)
);
