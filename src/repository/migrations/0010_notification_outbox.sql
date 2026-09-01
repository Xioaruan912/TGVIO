CREATE TABLE notification_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  aggregate_type TEXT NOT NULL DEFAULT 'runtime',
  aggregate_id INTEGER,
  dedupe_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','delivering','retry_wait','sent','dead')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL,
  claim_owner TEXT,
  claimed_at REAL,
  lease_until REAL,
  last_status INTEGER,
  error_code TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  sent_at REAL
);

CREATE INDEX idx_notification_outbox_due
ON notification_outbox(state, next_attempt_at, id);

CREATE INDEX idx_notification_outbox_lease
ON notification_outbox(state, lease_until, id);
