ALTER TABLE jobs ADD COLUMN claim_owner TEXT;
ALTER TABLE jobs ADD COLUMN claim_kind TEXT;
ALTER TABLE jobs ADD COLUMN heartbeat_at REAL;

CREATE INDEX idx_jobs_claim ON jobs(state, claim_kind, id);
