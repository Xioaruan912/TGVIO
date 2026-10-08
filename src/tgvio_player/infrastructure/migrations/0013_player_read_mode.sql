PRAGMA foreign_keys=ON;

-- How the Player reads archive media: through the WebDAV mount ('webdav', the
-- default and the only mode before this migration) or through direct links the
-- archive proxy hands out ('direct'). A missing row reads as 'webdav'.
CREATE TABLE player_read_mode (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    mode TEXT NOT NULL CHECK(mode IN ('webdav','direct')),
    updated_at INTEGER NOT NULL
);
