CREATE TABLE runtime_leases (
    lease_name TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    acquired_at INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE INDEX idx_runtime_leases_expires
ON runtime_leases(expires_at);

CREATE TABLE job_phase_claims (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    holder_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY(job_id, phase)
);

CREATE INDEX idx_job_phase_claims_expires
ON job_phase_claims(expires_at, phase);

CREATE TABLE job_schedule (
    accepted_order INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO job_schedule(job_id, accepted_at)
SELECT id, created_at
FROM jobs
ORDER BY created_at, rowid;

CREATE INDEX idx_job_schedule_job
ON job_schedule(job_id);
