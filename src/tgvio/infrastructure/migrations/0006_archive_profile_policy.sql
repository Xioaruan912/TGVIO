ALTER TABLE archive_packages
ADD COLUMN archive_profile_id TEXT NOT NULL DEFAULT 'primary';

ALTER TABLE archive_packages
ADD COLUMN archive_policy TEXT NOT NULL DEFAULT 'required'
CHECK (archive_policy IN ('required','best_effort'));

ALTER TABLE archive_packages
ADD COLUMN archive_policy_version INTEGER NOT NULL DEFAULT 1
CHECK (archive_policy_version >= 1);
