-- R2-18F3: draft-scoped publish style override.
-- Style priority at confirm time: accepted job snapshot > draft override > owner default > system default.
-- Storing the override on the draft keeps "same style re-post" from mutating the owner default.

ALTER TABLE collection_drafts ADD COLUMN style_json TEXT;
