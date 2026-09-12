CREATE TABLE intake_events (
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    item_index INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(source_chat_id, source_message_id)
);

CREATE INDEX idx_intake_events_job
ON intake_events(job_id, item_index);

CREATE TABLE collection_sessions (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','finalized','cancelled')),
    status_chat_id INTEGER,
    status_message_id INTEGER,
    finalized_job_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX idx_collection_sessions_open_owner_chat
ON collection_sessions(owner_id, chat_id)
WHERE state='open';

CREATE INDEX idx_collection_sessions_owner_state
ON collection_sessions(owner_id, state, updated_at);

CREATE TABLE collection_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES collection_sessions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    entry_kind TEXT NOT NULL CHECK(entry_kind IN ('media','text')),
    source_chat_id INTEGER,
    source_message_id INTEGER,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, ordinal)
);

CREATE UNIQUE INDEX idx_collection_entries_source
ON collection_entries(session_id, source_chat_id, source_message_id)
WHERE source_chat_id IS NOT NULL AND source_message_id IS NOT NULL;

CREATE INDEX idx_collection_entries_session_ordinal
ON collection_entries(session_id, ordinal);

CREATE TABLE user_preferences (
    owner_id INTEGER PRIMARY KEY,
    spoiler_mode TEXT NOT NULL DEFAULT 'source'
        CHECK(spoiler_mode IN ('source','ask','always_spoiler','always_normal')),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE job_display_messages (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    replacement_count INTEGER NOT NULL DEFAULT 0 CHECK(replacement_count >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
