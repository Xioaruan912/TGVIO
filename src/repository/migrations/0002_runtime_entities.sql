CREATE TABLE job_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  source_chat_id INTEGER,
  source_message_id INTEGER,
  grouped_id INTEGER,
  media_kind TEXT,
  original_name TEXT,
  mime_type TEXT,
  local_path TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  download_state TEXT NOT NULL DEFAULT 'pending',
  publish_state TEXT NOT NULL DEFAULT 'pending',
  source_descriptor BLOB,
  metadata_json TEXT,
  UNIQUE(job_id, ordinal)
);

CREATE INDEX idx_job_items_job ON job_items(job_id, ordinal);

CREATE TABLE job_texts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  UNIQUE(job_id, ordinal)
);

CREATE INDEX idx_job_texts_job ON job_texts(job_id, ordinal);

CREATE TABLE published_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  peer_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  created_at REAL NOT NULL,
  deleted_at REAL,
  UNIQUE(peer_id, message_id)
);

CREATE INDEX idx_published_messages_job ON published_messages(job_id, id);

CREATE TABLE interaction_sessions (
  user_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  field TEXT,
  payload_json TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  expires_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

ALTER TABLE backup_files RENAME TO backup_files_legacy;

CREATE TABLE backup_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES backup_attempts(id) ON DELETE CASCADE,
  job_item_id INTEGER REFERENCES job_items(id) ON DELETE SET NULL,
  local_path TEXT NOT NULL,
  remote_name TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  state TEXT NOT NULL,
  bytes_done INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  error_message TEXT,
  UNIQUE(attempt_id, remote_name)
);

INSERT INTO backup_files(
  id, attempt_id, job_item_id, local_path, remote_name, size_bytes,
  state, bytes_done, error_code, error_message
)
SELECT
  id, attempt_id, NULL, local_path, remote_name, size_bytes,
  state, bytes_done, error_code, error_message
FROM backup_files_legacy;

DROP TABLE backup_files_legacy;
CREATE INDEX idx_backup_files_attempt ON backup_files(attempt_id, id);
