CREATE TABLE archive_deletions (
    package_id TEXT PRIMARY KEY REFERENCES archive_packages(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'prepared'
        CHECK(state IN ('prepared','deleting','partial_failed','deleted')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    target_set_hash TEXT NOT NULL CHECK(length(target_set_hash) = 64),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TEXT
);

CREATE INDEX idx_archive_deletions_state
ON archive_deletions(state, updated_at);

CREATE TABLE archive_deletion_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES archive_deletions(package_id) ON DELETE CASCADE,
    target_index INTEGER NOT NULL CHECK(target_index >= 0),
    target_kind TEXT NOT NULL
        CHECK(target_kind IN ('commit_marker','object','manifest')),
    archive_object_id INTEGER REFERENCES archive_objects(id) ON DELETE RESTRICT,
    remote_path TEXT NOT NULL CHECK(length(remote_path) > 0),
    expected_size_bytes INTEGER NOT NULL CHECK(expected_size_bytes >= 0),
    expected_sha256 TEXT,
    expected_etag TEXT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','failed','deleted')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    error_code TEXT,
    verification_method TEXT,
    already_missing INTEGER NOT NULL DEFAULT 0 CHECK(already_missing IN (0,1)),
    deleted_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(
        (target_kind='object' AND archive_object_id IS NOT NULL)
        OR (target_kind IN ('commit_marker','manifest') AND archive_object_id IS NULL)
    ),
    UNIQUE(package_id, target_index),
    UNIQUE(package_id, remote_path),
    UNIQUE(package_id, archive_object_id)
);

CREATE INDEX idx_archive_deletion_targets_state
ON archive_deletion_targets(package_id, state, target_index);

CREATE TABLE archive_deletion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES archive_deletions(package_id) ON DELETE CASCADE,
    target_id INTEGER REFERENCES archive_deletion_targets(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL
        CHECK(event_type IN (
            'deletion_prepared',
            'deletion_started',
            'target_delete_started',
            'target_delete_succeeded',
            'target_delete_failed',
            'deletion_partial_failed',
            'deletion_completed'
        )),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_archive_deletion_events_package
ON archive_deletion_events(package_id, id);
