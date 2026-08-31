ALTER TABLE stat_metric_applied RENAME TO stat_metric_applied_legacy;

CREATE TABLE stat_metric_applied (
  scope_key TEXT NOT NULL,
  metric TEXT NOT NULL,
  day_utc TEXT NOT NULL,
  value INTEGER NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(scope_key, metric, day_utc)
);

INSERT INTO stat_metric_applied(scope_key,metric,day_utc,value,created_at)
SELECT scope_key,metric,day_utc,value,created_at
FROM stat_metric_applied_legacy;

DROP TABLE stat_metric_applied_legacy;
CREATE INDEX idx_stat_metric_day ON stat_metric_applied(day_utc, metric);
