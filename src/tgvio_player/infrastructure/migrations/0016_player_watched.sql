PRAGMA foreign_keys=ON;

-- Videos the viewer has watched (played to near the end), short or long. The
-- feed shows unseen videos first and the wall can filter on it. A mark counts
-- only for a while: player_watch_settings.forget_after_days, when set, makes
-- a video watched longer ago than that count as unseen again; NULL means marks
-- never expire. The window starts at half a month.
CREATE TABLE player_watched (
    media_id TEXT PRIMARY KEY REFERENCES media(media_id) ON DELETE CASCADE,
    watched_at INTEGER NOT NULL
);

CREATE TABLE player_watch_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    forget_after_days INTEGER CHECK(forget_after_days IS NULL OR forget_after_days > 0),
    updated_at INTEGER NOT NULL
);

INSERT INTO player_watch_settings(singleton, forget_after_days, updated_at)
VALUES(1, 15, CAST(strftime('%s','now') AS INTEGER));
