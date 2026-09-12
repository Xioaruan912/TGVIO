CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    destination TEXT NOT NULL,
    state TEXT NOT NULL,
    policy_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_owner_state ON jobs(owner_id, state);

CREATE TABLE IF NOT EXISTS job_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    item_index INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    local_path TEXT,
    name TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mime_type TEXT,
    width INTEGER,
    height INTEGER,
    duration_seconds REAL,
    container TEXT,
    codec TEXT,
    spoiler INTEGER NOT NULL DEFAULT 0,
    grouped_id INTEGER,
    source_chat_id INTEGER,
    source_message_id INTEGER,
    sha256 TEXT,
    telegram_ref TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(job_id, item_index)
);

CREATE INDEX IF NOT EXISTS idx_job_items_job ON job_items(job_id, item_index);
CREATE INDEX IF NOT EXISTS idx_job_items_sha256 ON job_items(sha256);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id);

CREATE TABLE IF NOT EXISTS publish_plans (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS publish_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES publish_plans(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    item_indexes_json TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending',
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plan_id, step_index)
);

CREATE INDEX IF NOT EXISTS idx_publish_steps_plan ON publish_steps(plan_id, step_index);
CREATE INDEX IF NOT EXISTS idx_publish_steps_state ON publish_steps(state);

CREATE TABLE IF NOT EXISTS publish_effects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES publish_plans(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    effect_type TEXT NOT NULL,
    external_chat_id TEXT,
    external_message_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_publish_effects_plan ON publish_effects(plan_id, step_index, id);

CREATE TABLE IF NOT EXISTS telegram_file_cache (
    sha256 TEXT NOT NULL,
    destination TEXT NOT NULL,
    media_kind TEXT NOT NULL,
    reference TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(sha256, destination, media_kind)
);

CREATE INDEX IF NOT EXISTS idx_telegram_file_cache_destination
ON telegram_file_cache(destination, media_kind, updated_at);

CREATE TABLE IF NOT EXISTS job_controls (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    cancel_reason TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS archive_packages (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    layout_version TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    staging_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    manifest_sha256 TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    committed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_archive_packages_state
ON archive_packages(state, updated_at);

CREATE TABLE IF NOT EXISTS archive_objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES archive_packages(id) ON DELETE CASCADE,
    object_index INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    local_path TEXT NOT NULL,
    remote_relpath TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    verification_method TEXT,
    remote_etag TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(package_id, object_index),
    UNIQUE(package_id, remote_relpath)
);

CREATE INDEX IF NOT EXISTS idx_archive_objects_state
ON archive_objects(state, next_retry_at, package_id);

CREATE TABLE IF NOT EXISTS archive_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES archive_packages(id) ON DELETE CASCADE,
    object_id INTEGER REFERENCES archive_objects(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_archive_events_package
ON archive_events(package_id, id);

CREATE TABLE IF NOT EXISTS job_progress (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    current_value INTEGER NOT NULL DEFAULT 0,
    total_value INTEGER NOT NULL DEFAULT 0,
    item_index INTEGER,
    item_total INTEGER NOT NULL DEFAULT 0,
    detail_code TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runtime_health (
    component TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
