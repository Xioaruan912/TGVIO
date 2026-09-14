-- R2-16: collection editing drafts, per-entry overlays and idempotent frozen submissions.
-- Adds a draft layer on top of the existing open/finalized/cancelled collection sessions
-- without changing the 0003 state CHECK. Saved drafts stay sessions in state 'open' but
-- are inactive; exactly one active collecting draft exists per (owner, chat).

CREATE TABLE IF NOT EXISTS collection_drafts (
    session_id TEXT PRIMARY KEY REFERENCES collection_sessions(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    editor_state TEXT NOT NULL DEFAULT 'collecting'
        CHECK(editor_state IN ('collecting','preview','saved','submitted','discarded')),
    cover_entry_id INTEGER,
    caption_override TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_collection_drafts_owner_active
ON collection_drafts(owner_id, active, updated_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_collection_drafts_active_owner_chat
ON collection_drafts(owner_id, chat_id)
WHERE active=1;

CREATE TABLE IF NOT EXISTS collection_entry_edits (
    entry_id INTEGER PRIMARY KEY REFERENCES collection_entries(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES collection_sessions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    excluded INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_collection_entry_edits_session
ON collection_entry_edits(session_id, position);

CREATE TABLE IF NOT EXISTS collection_submissions (
    session_id TEXT PRIMARY KEY REFERENCES collection_sessions(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    snapshot_hash TEXT NOT NULL,
    job_ids_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'creating'
        CHECK(state IN ('creating','created','failed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS editing_interactions (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    field TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_editing_interactions_owner
ON editing_interactions(owner_id, consumed_at, expires_at);

-- Backfill: every existing open session becomes an active collecting draft, and every
-- entry gets an overlay row mirroring its ordinal so reorder/remove operate uniformly.
INSERT OR IGNORE INTO collection_drafts(
    session_id, owner_id, chat_id, revision, editor_state, active
)
SELECT id, owner_id, chat_id, 1, 'collecting', 1
FROM collection_sessions WHERE state='open';

INSERT OR IGNORE INTO collection_entry_edits(entry_id, session_id, position, excluded)
SELECT id, session_id, ordinal, 0 FROM collection_entries;

-- Replace the old "one open session per owner/chat" rule with the active-draft rule
-- implemented above; saved drafts may remain open but inactive.
DROP INDEX IF EXISTS idx_collection_sessions_open_owner_chat;
