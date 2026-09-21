-- R2-45: one durable, idempotent recovery child for skipped items of a terminal Job.
-- Intake keys intentionally remain attached to the parent; this relation is not intake.

CREATE TABLE item_recovery_jobs (
    parent_job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    child_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
