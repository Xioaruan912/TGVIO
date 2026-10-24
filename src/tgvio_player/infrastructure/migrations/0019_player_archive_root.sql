PRAGMA foreign_keys=ON;

-- The Archive folder the catalog is read from, set in the Player's storage
-- settings page instead of the environment. Empty means not set yet: catalog
-- sync waits rather than scanning the WebDAV root.
ALTER TABLE player_storage_settings ADD COLUMN archive_root TEXT NOT NULL DEFAULT '';
