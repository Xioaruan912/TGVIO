-- R2-18F2: durable frozen submission and per-part idempotent job identity.
-- Adds the persisted frozen snapshot + owning token to existing submissions and a
-- stable (session_id, part_index) -> job_id map so crash recovery never re-creates
-- or loses an already-created chunk.

ALTER TABLE collection_submissions ADD COLUMN frozen_json TEXT;
ALTER TABLE collection_submissions ADD COLUMN token_id TEXT;

CREATE INDEX IF NOT EXISTS idx_collection_submissions_token
ON collection_submissions(token_id);

CREATE TABLE IF NOT EXISTS collection_part_jobs (
    session_id TEXT NOT NULL REFERENCES collection_sessions(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(session_id, part_index)
);
