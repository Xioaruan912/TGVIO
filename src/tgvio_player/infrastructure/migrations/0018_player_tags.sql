PRAGMA foreign_keys=ON;

-- The viewer's own labels. A video can carry any number of them and the wall
-- can be narrowed to one. Names are unique regardless of letter case.
CREATE TABLE player_tags (
    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at INTEGER NOT NULL
);

CREATE TABLE player_media_tags (
    media_id TEXT NOT NULL REFERENCES media(media_id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES player_tags(tag_id) ON DELETE CASCADE,
    tagged_at INTEGER NOT NULL,
    PRIMARY KEY(media_id, tag_id)
);

CREATE INDEX idx_player_media_tags_tag ON player_media_tags(tag_id);
