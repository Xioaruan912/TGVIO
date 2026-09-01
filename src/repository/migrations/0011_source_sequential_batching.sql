ALTER TABLE source_profiles
ADD COLUMN sequential_video_gather_seconds REAL NOT NULL DEFAULT 120.0
CHECK(sequential_video_gather_seconds BETWEEN 1.0 AND 600.0);

CREATE INDEX idx_source_events_received_created
ON source_events(state, created_at, id);
