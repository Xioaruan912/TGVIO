-- R2-18E/R2-18D: organization suggestions (with durable undo) and bounded preview requests.

CREATE TABLE IF NOT EXISTS suggestion_applications (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES collection_sessions(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    revision_applied INTEGER NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_suggestion_applications_session
ON suggestion_applications(session_id, consumed, created_at DESC);

CREATE TABLE IF NOT EXISTS preview_requests (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','running','succeeded','failed','cancelled','expired')),
    cover_entry_id INTEGER,
    cache_dir TEXT,
    result_json TEXT,
    error_code TEXT,
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_preview_requests_owner
ON preview_requests(owner_id, state, created_at DESC);
