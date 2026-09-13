CREATE TABLE operation_tokens (
    token TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_operation_tokens_owner_expiry
ON operation_tokens(owner_id, expires_at);

CREATE TABLE publish_effect_revocations (
    effect_id INTEGER PRIMARY KEY REFERENCES publish_effects(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','deleted','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    deleted_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE publish_effect_revocation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_id INTEGER NOT NULL REFERENCES publish_effects(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL
        CHECK(event_type IN ('delete_succeeded','delete_failed')),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_publish_effect_revocation_events_effect
ON publish_effect_revocation_events(effect_id, id);
