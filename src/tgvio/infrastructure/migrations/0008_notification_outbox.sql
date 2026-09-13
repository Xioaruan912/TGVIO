-- R2-09: durable, redacted notification outbox for the read-only operations surface.

CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    next_attempt_at REAL NOT NULL,
    claimed_by TEXT,
    claim_expires_at REAL,
    last_error_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    sent_at REAL
);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
ON notification_outbox(state, next_attempt_at);
