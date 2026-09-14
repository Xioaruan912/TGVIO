-- R2-15B: per-business-day display numbers (independent of monotonic accepted_order).

CREATE TABLE IF NOT EXISTS job_display_identity (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    business_day TEXT NOT NULL,
    display_no INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(business_day, display_no)
);

CREATE INDEX IF NOT EXISTS idx_job_display_identity_day
ON job_display_identity(business_day, display_no);

-- Deterministic backfill for Jobs created before this migration. The Beijing
-- business day starts at 06:00 CST, i.e. date(created_at UTC + 2 hours).
INSERT OR IGNORE INTO job_display_identity(job_id, business_day, display_no, created_at)
SELECT
    j.id,
    date(j.created_at, '+2 hours') AS business_day,
    ROW_NUMBER() OVER (
        PARTITION BY date(j.created_at, '+2 hours')
        ORDER BY j.created_at, s.accepted_order, j.rowid
    ) AS display_no,
    CAST(strftime('%s', j.created_at) AS INTEGER) AS created_at
FROM jobs j
LEFT JOIN job_schedule s ON s.job_id = j.id;
