PRAGMA foreign_keys=ON;

-- The folders the catalog reads, in the order the settings page shows them.
-- The first one also holds the backup (<root>/收藏); archive_root mirrors it.
CREATE TABLE player_libraries (
    position INTEGER PRIMARY KEY CHECK(position >= 0),
    root TEXT NOT NULL UNIQUE
);

INSERT INTO player_libraries(position, root)
SELECT 0, archive_root FROM player_storage_settings WHERE archive_root != '';
