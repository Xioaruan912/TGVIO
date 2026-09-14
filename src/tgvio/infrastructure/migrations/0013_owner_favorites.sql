-- R2-18B: owner-scoped favorites and quieter notifications/publish preferences.

ALTER TABLE user_preferences ADD COLUMN quiet_mode INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user_preferences ADD COLUMN style_json TEXT;

CREATE TABLE IF NOT EXISTS favorites (
    owner_id INTEGER NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(owner_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_favorites_owner_created
ON favorites(owner_id, created_at DESC, job_id);
