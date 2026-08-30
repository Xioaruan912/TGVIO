ALTER TABLE jobs ADD COLUMN legacy_seq INTEGER;

CREATE UNIQUE INDEX idx_jobs_legacy_seq
ON jobs(legacy_seq)
WHERE legacy_seq IS NOT NULL;
