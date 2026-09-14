-- R2-15A: safe history maintenance (hide instead of delete; durable run/targets).

CREATE TABLE IF NOT EXISTS job_visibility (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    hidden_at REAL NOT NULL,
    reason TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_job_visibility_hidden
ON job_visibility(hidden_at, job_id);

CREATE TABLE IF NOT EXISTS maintenance_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    business_day TEXT NOT NULL,
    cutoff_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    generation INTEGER NOT NULL DEFAULT 1,
    lease_until REAL,
    holder_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL,
    normalized_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE(kind, business_day)
);

CREATE INDEX IF NOT EXISTS idx_maintenance_runs_due
ON maintenance_runs(status, next_retry_at);

CREATE TABLE IF NOT EXISTS maintenance_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES maintenance_runs(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    display_chat_id INTEGER,
    display_message_id INTEGER,
    phase TEXT NOT NULL DEFAULT 'pending',
    status TEXT NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    normalized_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(run_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_maintenance_targets_status
ON maintenance_targets(run_id, status);
