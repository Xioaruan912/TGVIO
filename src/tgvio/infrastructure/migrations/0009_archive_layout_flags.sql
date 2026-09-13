-- R2-13: durable per-day Archive layout counter and runtime feature flags.

CREATE TABLE IF NOT EXISTS archive_day_counters (
    day TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS runtime_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
