ALTER TABLE favorite_sync
ADD COLUMN intent_persisted INTEGER NOT NULL DEFAULT 0 CHECK(intent_persisted IN (0,1));
