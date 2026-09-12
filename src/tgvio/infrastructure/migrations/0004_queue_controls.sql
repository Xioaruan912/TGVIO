ALTER TABLE job_controls ADD COLUMN hold_requested INTEGER NOT NULL DEFAULT 0 CHECK(hold_requested IN (0,1));
ALTER TABLE job_controls ADD COLUMN hold_reason TEXT;
ALTER TABLE job_controls ADD COLUMN hold_revision INTEGER NOT NULL DEFAULT 0;

CREATE TABLE queue_controls (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
    pause_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO queue_controls(singleton, paused, revision) VALUES(1, 0, 0);
