CREATE TABLE daily_stats (
  day_utc TEXT PRIMARY KEY,
  accepted_jobs INTEGER NOT NULL DEFAULT 0,
  succeeded_jobs INTEGER NOT NULL DEFAULT 0,
  failed_jobs INTEGER NOT NULL DEFAULT 0,
  cancelled_jobs INTEGER NOT NULL DEFAULT 0,
  downloaded_bytes INTEGER NOT NULL DEFAULT 0,
  published_bytes INTEGER NOT NULL DEFAULT 0,
  backed_up_bytes INTEGER NOT NULL DEFAULT 0,
  saved_upload_bytes INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);

CREATE TABLE stat_metric_applied (
  scope_key TEXT NOT NULL,
  metric TEXT NOT NULL,
  day_utc TEXT NOT NULL,
  value INTEGER NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(scope_key, metric, day_utc)
);

CREATE INDEX idx_stat_metric_day ON stat_metric_applied(day_utc, metric);
